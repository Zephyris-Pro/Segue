"""Forzify settings - mixer-style editor for the shared runtime config.

The window edits the SAME Config object the runtime reads (live), and also
persists to disk so changes survive restarts and external edits. It no longer
loads its own config or owns a QApplication; the unified app (`fh6_spotify.app`)
constructs it and wires close behaviour.

Native Windows frame (titlebar buttons, edge outline, drop shadow, rounded
corners) - and native frames keep ClearType text crisp. Closing the window (✕)
quits the whole app; minimize goes to the taskbar.
"""
import os
import sys
import math
import random
import time
import ctypes
from ctypes import wintypes
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QRectF, QPoint, QEvent, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import QGraphicsDropShadowEffect
from PySide6.QtGui import QColor, QPainter, QFont, QIcon, QPixmap, QPainterPath, QPen, QFontMetrics, QLinearGradient, QGuiApplication, QMovie
from PySide6.QtWidgets import QWidget, QLabel, QSlider, QCheckBox, QAbstractButton, QPushButton, QVBoxLayout, QHBoxLayout, QFrame, QMenu, QMessageBox, QScrollArea, QInputDialog, QDialog, QListWidget, QStackedWidget, QSizePolicy, QToolButton, QGridLayout, QComboBox, QLineEdit, QProxyStyle, QStyle, QStyledItemDelegate
from fh6_spotify import presets as _presets
from fh6_spotify import input_backend as _ib
from fh6_spotify import mediakeys as _mk
from fh6_spotify import autostart as _autostart
from fh6_spotify import updater as _updater
from fh6_spotify.version import VERSION as _APP_VERSION
from fh6_spotify.version import VERSION_LABEL as _APP_VERSION_LABEL
from fh6_spotify.theme import c as _c, set_theme as _set_theme, load_theme as _load_theme, save_theme as _save_theme, active_theme as _active_theme, ACCENT as _ACCENT
def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


_TITLEBAR_H = 34
_SCALE = 1.0
_SCALE_STEPS = [1.25, 1.5, 1.75]


def _scale_label(step: float) -> str:
    return f'{int(round((step - 0.25) * 100))}%'


def _s(n: int) -> int:
    return max(1, int(round(n * _SCALE)))


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long), ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [('rgrc', _RECT * 3), ('lppos', ctypes.c_void_p)]


class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint), ('flags', ctypes.c_uint), ('showCmd', ctypes.c_uint), ('ptMinPosition', _POINT), ('ptMaxPosition', _POINT), ('rcNormalPosition', _RECT)]


_LOOP_DT = 0.02


def _ramp_from_ms(ms: int) -> float:
    secs = max(50, ms) / 1000.0
    return 1.0 - 0.05 ** (_LOOP_DT / secs)


def _ms_from_ramp(ramp: float) -> int:
    ramp = min(max(ramp, 0.0001), 0.999)
    n = math.log(0.05) / math.log(1.0 - ramp)
    return int(round(n * _LOOP_DT * 1000))


def _ui_font(px: int, weight=QFont.Medium) -> QFont:
    """Segoe UI Variable (the native Win11 UI font, hand-hinted for small sizes)
    forced to GRAYSCALE antialiasing + full hinting.

    Two Qt defaults make text look 'choppy/dirty' vs native apps:
      * ClearType *subpixel* AA paints colored (orange/blue) fringes on stems;
        NoSubpixelAntialias switches to clean grayscale AA like Win11 apps.
      * weak hinting lets stems land between pixels (uneven weight); full hinting
        grid-fits them. Segoe is built for this, Inter is not.
    The strategy/hint must live on a QFont set programmatically: a QSS
    `font-family` rule rebuilds the font and silently drops both."""
    f = QFont('Segoe UI')
    f.setPixelSize(_s(px))
    f.setWeight(weight)
    f.setStyleStrategy(QFont.PreferAntialias | QFont.NoSubpixelAntialias)
    f.setHintingPreference(QFont.PreferFullHinting)
    return f


def _link_pixmap(connected: bool) -> QPixmap:
    """Horizontal chain-link glyph (AA vector, thick solid rings). Connected ->
    bright + interlocked; disconnected -> dim + rings pulled apart (broken)."""
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_c('icon') if connected else _c('icon_dim')))
    p.translate(s / 2, s / 2)
    off = 11.0 if connected else 19.0

    def ring(cx: float) -> QPainterPath:
        pth = QPainterPath()
        pth.setFillRule(Qt.OddEvenFill)
        pth.addRoundedRect(QRectF(cx - 16, -10, 32, 20), 10, 10)
        pth.addRoundedRect(QRectF(cx - 9, -4, 18, 8), 4, 4)
        return pth
    p.drawPath(ring(-off))
    p.drawPath(ring(off))
    p.end()
    return pix.scaled(_s(24), _s(24), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _app_icon(path: str, letter: str, size: int = 28) -> QPixmap:
    """Load an app logo (official asset the user drops in assets/). Falls back to
    a neutral rounded-square placeholder with a letter when the file is absent."""
    if os.path.exists(path):
        pm = QPixmap(path)
        if not pm.isNull():
            return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    s = size * 2
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_c('border')))
    p.drawRoundedRect(0, 0, s, s, s * 0.28, s * 0.28)
    p.setPen(QColor(_c('text')))
    p.setFont(_ui_font(int(size), QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, letter)
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _exe_icon(path: str, letter: str, size: int = 22) -> QIcon:
    """The real icon embedded in an exe (the app's own logo), or a letter-avatar
    fallback. Used by the custom-source picker so each app shows its real icon
    instead of a generic letter tile."""
    if path:
        try:
            from PySide6.QtWidgets import QFileIconProvider
            from PySide6.QtCore import QFileInfo
            ic = QFileIconProvider().icon(QFileInfo(path))
            if ic is not None and not ic.isNull():
                pm = ic.pixmap(_s(size), _s(size))
                if not pm.isNull():
                    return QIcon(pm)
        except Exception:
            pass
    return QIcon(_app_icon('', letter, size))


def _custom_glyph_pixmap(size: int = 22) -> QPixmap:
    """Icon for the 'Custom…' source entry before a real app is picked: the
    rounded tile (matching the letter avatars) with a '+' - you're adding your
    own source. Once a real app is picked it shows that app's own icon instead."""
    s = size * 2
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    _tp = QPen(QColor(_c('border_hi')))
    _tp.setWidthF(max(1.0, s * 0.045))
    p.setPen(_tp)
    p.setBrush(QColor(_c('surface')))
    _in = _tp.widthF() / 2
    p.drawRoundedRect(QRectF(_in, _in, s - 2 * _in, s - 2 * _in), s * 0.26, s * 0.26)
    pen = QPen(QColor(_c('icon')))
    pen.setWidthF(max(1.6, s * 0.1))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    c = s / 2
    r = s * 0.22
    p.drawLine(int(c - r), int(c), int(c + r), int(c))
    p.drawLine(int(c), int(c - r), int(c), int(c + r))
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _dots_tile_pixmap(size: int = 22) -> QPixmap:
    """'More' submenu icon: the same rounded tile as the Custom entry, with three
    dots, so the two custom/more rows match visually."""
    s = size * 2
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    _tp = QPen(QColor(_c('border_hi')))
    _tp.setWidthF(max(1.0, s * 0.045))
    p.setPen(_tp)
    p.setBrush(QColor(_c('surface')))
    _in = _tp.widthF() / 2
    p.drawRoundedRect(QRectF(_in, _in, s - 2 * _in, s - 2 * _in), s * 0.26, s * 0.26)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_c('icon')))
    r = s * 0.075
    for dx in (-s * 0.2, 0.0, s * 0.2):
        p.drawEllipse(QRectF(s / 2 + dx - r, s / 2 - r, 2 * r, 2 * r))
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _alpha_bbox_center(pm, ss):
    """Logical centre of a pixmap's opaque (alpha > 8) region, or None if fully
    transparent. Used to find where CE_PushButtonLabel actually drew the glyph in a
    buffer, so it can be offset onto the full-draw position."""
    from PySide6.QtGui import QImage
    from PySide6.QtCore import QPointF
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None
    mv = img.bits()
    stride = img.bytesPerLine()
    minx = miny = 1073741824
    maxx = maxy = -1
    for y in range(h):
        base = y * stride + 3
        for x in range(w):
            if mv[base + x * 4] > 8:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return None
    return QPointF((minx + maxx + 1) / 2.0 / ss, (miny + maxy + 1) / 2.0 / ss)


def _style_icon_center(btn, opt, ss):
    """Logical centre of where the STYLE actually draws this button's icon, found by
    rendering the full control and the control-without-icon and taking the centre of
    the region that differs. CE_PushButtonLabel alone mis-places the icon on a
    QSS-grown active tab (it ignores the grow, drawing the glyph lower), which made
    the press dip scale around the wrong point and drag the glyph down. The full
    CE_PushButton draws the icon at its true spot; the diff reads exactly that, so
    the dip's glyph centre matches the resting glyph and it scales in place.
    Returns None if nothing differs (no icon)."""
    from PySide6.QtGui import QPixmap, QIcon, QImage
    from PySide6.QtWidgets import QStyle, QStylePainter
    from PySide6.QtCore import Qt, QPointF
    w = max(1, int(btn.width() * ss))
    h = max(1, int(btn.height() * ss))
    full = QPixmap(w, h)
    full.setDevicePixelRatio(ss)
    full.fill(Qt.transparent)
    bg = QPixmap(w, h)
    bg.setDevicePixelRatio(ss)
    bg.fill(Qt.transparent)
    sp = QStylePainter(full, btn)
    sp.drawControl(QStyle.ControlElement.CE_PushButton, opt)
    sp.end()
    saved = opt.icon
    opt.icon = QIcon()
    sp = QStylePainter(bg, btn)
    sp.drawControl(QStyle.ControlElement.CE_PushButton, opt)
    sp.end()
    opt.icon = saved
    ia = full.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    ib = bg.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    iw = min(ia.width(), ib.width())
    ih = min(ia.height(), ib.height())
    ma, mb = ia.bits(), ib.bits()
    sa, sb = ia.bytesPerLine(), ib.bytesPerLine()
    minx = miny = 1073741824
    maxx = maxy = -1
    for y in range(ih):
        ra, rb = y * sa, y * sb
        for x in range(iw):
            ja, jb = ra + x * 4, rb + x * 4
            if abs(ma[ja] - mb[jb]) + abs(ma[ja + 1] - mb[jb + 1]) + abs(ma[ja + 2] - mb[jb + 2]) + abs(ma[ja + 3] - mb[jb + 3]) > 40:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return None
    return QPointF((minx + maxx + 1) / 2.0 / ss, (miny + maxy + 1) / 2.0 / ss)


_DEV_ART = {'playstation': ('dualsense.png', False), 'dualsense': ('dualsense.png', False), 'dualshock': ('dualsense.png', False), 'xbox': ('xbox controller v4.png', True), 'keyboard': ('keyboard icon v5.png', True), 'wheel': ('steering-wheel v3.png', True)}


def _dev_asset(dev: str) -> str:
    """Path to the device's icon art, or '' if none ships."""
    name = _DEV_ART.get(dev, ('', False))[0]
    if name:
        p = os.path.join(_ASSETS, name)
        if os.path.exists(p):
            return p
    return ''


def _dev_name(dev: str) -> str:
    """Friendly device label for the Controls pill + tooltips."""
    return {'playstation': 'PlayStation', 'dualsense': 'PlayStation', 'dualshock': 'PlayStation', 'xbox': 'Xbox', 'keyboard': 'Keyboard', 'wheel': 'Sim wheel'}.get(dev, 'Controller')


def _dev_wm_scale(dev: str) -> float:
    """Controls-pill watermark size multiple. The DualSense art fills its box; the
    line-art icons (xbox / keyboard / wheel) sit smaller, so they get a bump."""
    return 1.5 if dev in ('playstation', 'dualsense', 'dualshock') else 2.0


def _invert_rgb(pm):
    """Invert RGB (alpha untouched) - the v2 device art is black line-art, so we
    flip it to white to read on the dark UI."""
    if pm is None or pm.isNull():
        return pm
    from PySide6.QtGui import QImage
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    img.invertPixels(QImage.InvertRgb)
    return QPixmap.fromImage(img)


def _recolor_pm(pm, color: str):
    """Flat-recolor a pixmap to `color`, keeping its alpha (SourceAtop fill).
    For monochrome line-art glyphs where only the silhouette matters."""
    if pm is None or pm.isNull():
        return pm
    out = QPixmap(pm.size())
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.drawPixmap(0, 0, pm)
    p.setCompositionMode(QPainter.CompositionMode_SourceAtop)
    p.fillRect(out.rect(), QColor(color))
    p.end()
    return out


def _forza_pixmap(size: int) -> QPixmap:
    """Themed Forza badge. Dark/HC keep the asset as-is (black FH on white disc).
    Light INVERTS it (white FH on black disc) AND lifts the now-pure-black disc to
    a softer #424240 - pure black read harsh. None if the asset is missing."""
    pm = _load_scaled(_FORZA, size)
    if pm is None or pm.isNull():
        return pm
    if _active_theme() != 'light':
        return pm
    pm = _invert_rgb(pm)
    out = QPixmap(pm.size())
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.drawPixmap(0, 0, pm)
    p.setCompositionMode(QPainter.CompositionMode_Screen)
    p.fillRect(out.rect(), QColor('#424240'))
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def _dev_pixmap(dev: str, size: int):
    """Scaled device glyph, themed: WHITE on the dark UI, DARK on light. Art
    ships as black line-art (v2/v3) for most devices, already-light art for
    DualSense - so invert exactly when the art's tone differs from what the
    theme needs (XOR of "art is black" and "theme is light"). None when no art
    ships for `dev`."""
    pm = _load_scaled(_dev_asset(dev), size)
    if pm is None:
        return pm
    if _active_theme() == 'light':
        pm = _recolor_pm(pm, '#3c3c38')
        return pm
    if _DEV_ART.get(dev, ('', False))[1]:
        pm = _invert_rgb(pm)
    return pm


def _dev_qicon(dev: str, size: int):
    """QIcon form of _dev_pixmap, or None."""
    pm = _dev_pixmap(dev, size)
    if pm is not None and not pm.isNull():
        return QIcon(pm)


def _dev_icon(dev: str, size: int = 34) -> QIcon:
    """Generic device glyph (gamepad / wheel / keyboard). Overridden by an
    official logo if assets/<dev>.png exists (loaded by the caller)."""
    s = 44
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(_c('icon')))
    pen.setWidthF(2.4)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if dev == 'wheel':
        p.drawEllipse(QRectF(8, 8, 28, 28))
        p.setBrush(QColor(_c('icon')))
        p.drawEllipse(QRectF(19, 19, 6, 6))
        p.setBrush(Qt.NoBrush)
        p.drawLine(22, 22, 22, 9)
        p.drawLine(22, 22, 11, 30)
        p.drawLine(22, 22, 33, 30)
    elif dev == 'keyboard':
        p.drawRoundedRect(QRectF(6, 13, 32, 18), 4, 4)
        p.setBrush(QColor(_c('icon')))
        p.setPen(Qt.NoPen)
        for kx in (11, 17, 23, 29):
            p.drawRoundedRect(QRectF(kx, 18, 3, 3), 1, 1)
        p.drawRoundedRect(QRectF(14, 24, 16, 3), 1, 1)
    else:
        p.drawRoundedRect(QRectF(7, 15, 30, 15), 8, 8)
        p.setBrush(QColor(_c('icon')))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(13, 20, 5, 5))
        p.drawEllipse(QRectF(26, 20, 5, 5))
    p.end()
    return QIcon(pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _action_icon(kind: str, color: str, size: int = 16) -> QIcon:
    """lock / power / rebind glyphs in a given color (light for dark bg, dark
    when the toggle is active/white)."""
    s = 40
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c)
    pen.setWidthF(3.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    if kind == 'lock':
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(13, 7, 14, 17), 0, 2880)
        p.drawLine(13, 15, 13, 19)
        p.drawLine(27, 15, 27, 19)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(10, 19, 20, 15), 4, 4)
    elif kind == 'power':
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(11, 12, 18, 18), 2000, -4640)
        p.drawLine(20, 9, 20, 21)
    else:
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(6, 14, 28, 16), 8, 8)
        p.drawLine(13, 22, 19, 22)
        p.drawLine(16, 19, 16, 25)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(24, 18, 4, 4))
        p.drawEllipse(QRectF(28, 22, 4, 4))
    p.end()
    return QIcon(pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _kind_icon(kind: str, color: str) -> QIcon:
    """Action glyph in `color`: the official asset (assets/<kind>.png, e.g. power)
    recoloured via its alpha, else the drawn fallback."""
    name = 'lock icon v2' if kind == 'lock' else kind
    t = _tinted(os.path.join(_ASSETS, name + '.png'), color, 18)
    if t is not None:
        return QIcon(t)
    return _action_icon(kind, color)


def _tab_icon(kind: str, size: int = 20, color: str = None) -> QIcon:
    """Tab glyphs: 'mixer' = horizontal faders, 'extras' = settings gear. Both
    prefer a bundled asset recoloured to `color`, falling back to a drawn glyph
    when the PNG is missing. color defaults to the active theme's icon colour."""
    color = color or _c('icon')
    if kind == 'extras':
        t = _tinted(os.path.join(_ASSETS, 'Overlay icon v4.png'), color, int(size * 1.3))
        if t is not None:
            return QIcon(t)
    if kind == 'mixer':
        t = _tinted(os.path.join(_ASSETS, 'Mixer new icon v3.png'), color, size)
        if t is not None:
            return QIcon(t)
    s = 40
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    if kind == 'mixer':
        pen = QPen(c)
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.RoundCap)
        for kx, y in ((12, 27), (20, 13), (28, 22)):
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawLine(8, y, 32, y)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(QRectF(kx - 4, y - 4, 8, 8))
    else:
        cx = cy = 20.0
        teeth = QPen(c)
        teeth.setWidthF(4.6)
        teeth.setCapStyle(Qt.RoundCap)
        p.setPen(teeth)
        p.setBrush(Qt.NoBrush)
        for i in range(8):
            a = math.radians(i * 45)
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(int(cx + 8.5 * ca), int(cy + 8.5 * sa), int(cx + 13.0 * ca), int(cy + 13.0 * sa))
        ring = QPen(c)
        ring.setWidthF(3.2)
        p.setPen(ring)
        p.drawEllipse(QRectF(cx - 8.5, cy - 8.5, 17, 17))
        p.drawEllipse(QRectF(cx - 3.4, cy - 3.4, 6.8, 6.8))
    p.end()
    return QIcon(pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _tag_colors(tag: str) -> tuple:
    """(background, text) for the device-card tag pill. Scales the urgency:
    orange = first-class, white = solid-but-newer, gray = unknown territory.
    Unknown tags fall back to the orange accent so a typo is still visible."""
    t = (tag or '').lower()
    if t == 'best':
        return (QColor(_ACCENT), QColor(_c('emph_text')))
    if t == 'beta':
        return (QColor(_c('emph_fill')), QColor(_c('emph_text')))
    if t == 'untested':
        return (QColor(_c('text_disabled')), QColor(_c('text')))
    return (QColor(_ACCENT), QColor(_c('emph_text')))


class _DeviceButton(QPushButton):
    """Device-selector button that paints a small corner tag (e.g. 'Best',
    'Beta', 'Untested') over the icon. Tag colour scales with the tag text
    so the user can read the rough confidence at a glance."""

    def __init__(self, tag: str = ''):
        super().__init__()
        self._tag = tag

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._tag:
            return None
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(_ui_font(10, QFont.Bold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self._tag)
        pad_x, pad_y = _s(6), _s(2)
        w = tw + 2 * pad_x
        h = fm.height() + 2 * pad_y
        rect = QRectF(self.width() - w - _s(4), _s(4), w, h)
        bg, fg = _tag_colors(self._tag)
        _gap = float(_s(2))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_c('surface')))
        p.drawRoundedRect(rect.adjusted(-_gap, -_gap, _gap, _gap), (h + 2 * _gap) / 2, (h + 2 * _gap) / 2)
        p.setBrush(bg)
        p.drawRoundedRect(rect, h / 2, h / 2)
        p.setPen(fg)
        p.drawText(rect, Qt.AlignCenter, self._tag)
        p.end()


class _SkipModeButton(QPushButton):
    """Skip-input toggle (D-pad / Touchpad swipe). Active-state visual is
    driven purely by QSS (skipbtn[active=true] -> orange outline + white text,
    matching the bind key-caps). Earlier versions painted a corner badge or
    filled the whole pill orange; the outlined look reads as 'engaged' the same
    way a bound cap does, without clipping the centred label."""


class _FadeMenu(QMenu):
    """QMenu that fades in when shown - an instant pop felt abrupt; switching
    between submenus looked flickery. Opacity is forced to 0 in aboutToShow (i.e.
    BEFORE the native popup is mapped, so there's no full-opacity flash frame),
    then eased to 1. Any in-flight fade is stopped first so rapid hover-switches
    don't stack two animations (the flicker)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _round_menu(self)
        self.aboutToShow.connect(lambda: self.setWindowOpacity(0.0))

    def showEvent(self, e):
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        super().showEvent(e)
        prev = getattr(self, '_fade_anim', None)
        if prev is not None:
            prev.stop()
        a = QPropertyAnimation(self, b'windowOpacity', self)
        a.setDuration(130)
        a.setStartValue(self.windowOpacity())
        a.setEndValue(1.0)
        a.setEasingCurve(QEasingCurve.OutQuad)
        a.start()
        self._fade_anim = a


def _media_icon(kind: str, color: str = None, size: int = 18) -> QIcon:
    """Media-transport glyphs: shuffle / prev / play / pause / next / repeat.
    color defaults to the active theme's icon colour."""
    color = color or _c('icon')
    s = 40
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c)
    pen.setWidthF(3.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    if kind == 'play':
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(15, 11)
        path.lineTo(15, 29)
        path.lineTo(30, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == 'pause':
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(15, 11, 4.5, 18), 2, 2)
        p.drawRoundedRect(QRectF(21.5, 11, 4.5, 18), 2, 2)
    elif kind == 'next':
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(13, 11)
        path.lineTo(13, 29)
        path.lineTo(26, 20)
        path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(27, 11, 4, 18), 1.5, 1.5)
    elif kind == 'prev':
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(27, 11)
        path.lineTo(27, 29)
        path.lineTo(14, 20)
        path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(9, 11, 4, 18), 1.5, 1.5)
    elif kind == 'shuffle':
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        a = QPainterPath()
        a.moveTo(8, 13)
        a.lineTo(17, 13)
        a.lineTo(29, 27)
        b = QPainterPath()
        b.moveTo(8, 27)
        b.lineTo(17, 27)
        b.lineTo(29, 13)
        p.drawPath(a)
        p.drawPath(b)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for tip, k1, k2 in (((31, 27), (24, 27), (30, 20)), ((31, 13), (24, 13), (30, 20))):
            h = QPainterPath()
            h.moveTo(*tip)
            h.lineTo(*k1)
            h.lineTo(*k2)
            h.closeSubpath()
            p.drawPath(h)
    elif kind == 'repeat':
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(11, 12, 18, 16), 1248, 4672)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        h = QPainterPath()
        h.moveTo(30, 13)
        h.lineTo(22, 11)
        h.lineTo(26, 18)
        h.closeSubpath()
        p.drawPath(h)
    p.end()
    return QIcon(pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _menu_icon() -> QIcon:
    """Hamburger (3 rounded lines): bundled asset recoloured, else drawn."""
    t = _tinted(os.path.join(_ASSETS, 'hamburger menu icon.png'), _c('icon'), 20)
    if t is not None:
        return QIcon(t)
    s = 44
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_c('icon')))
    for cy in (14, 22, 30):
        p.drawRoundedRect(QRectF(10, cy - 2, 24, 4), 2, 2)
    p.end()
    return QIcon(pix.scaled(_s(20), _s(20), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _caption_icon(kind: str, size: int = 18, color: str = None) -> QIcon:
    """Win11-style caption glyph (min / max / restore / close) as AA vector.

    `color` overrides the glyph tint - used to flip the close X to white while
    its red hover background is showing (a dark themed X on red read muddy)."""
    s = 40
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color or _c('icon')))
    pen.setWidthF(2.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if kind == 'min':
        p.drawLine(10, 20, 30, 20)
    elif kind == 'max':
        p.drawRect(11, 11, 18, 18)
    elif kind == 'restore':
        p.drawRect(11, 15, 14, 14)
        p.drawLine(15, 11, 29, 11)
        p.drawLine(29, 11, 29, 25)
    elif kind == 'close':
        p.drawLine(10, 10, 30, 30)
        p.drawLine(30, 10, 10, 30)
    p.end()
    return QIcon(pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _info_icon(size: int = 15, bright: bool = False) -> QPixmap:
    """Small circled-i info glyph (AA vector). Hover target for tooltips."""
    s = size * 3
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor('#c4c4c2' if bright else '#8a8a88')
    pen = QPen(col)
    pen.setWidthF(s * 0.075)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    m = s * 0.1
    p.drawEllipse(QRectF(m, m, s - 2 * m, s - 2 * m))
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    cx = s / 2
    p.drawEllipse(QRectF(cx - s * 0.055, s * 0.3, s * 0.11, s * 0.11))
    p.drawRoundedRect(QRectF(cx - s * 0.055, s * 0.45, s * 0.11, s * 0.26), s * 0.05, s * 0.05)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _zoom_icon(path: str, sz: int, zoom: float = 1.0) -> QPixmap:
    """Menu icon scaled to `sz`, but enlarged by `zoom` and centre-cropped so
    logos with built-in padding (TIDAL, YT Music) fill the box like the others."""
    pm = QPixmap(path)
    if pm.isNull():
        return pm
    big = pm.scaled(int(sz * zoom), int(sz * zoom), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if zoom <= 1.0:
        return big
    out = QPixmap(sz, sz)
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.drawPixmap(int((sz - big.width()) / 2), int((sz - big.height()) / 2), big)
    p.end()
    return out


def _dim_pixmap(pm: QPixmap) -> QPixmap:
    """Faded copy used when that side is disconnected."""
    out = QPixmap(pm.size())
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setOpacity(0.3)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def _dev_wm_rot(dev: str, size: int = 90, angle: float = 14.0):
    """Device glyph rotated slightly clockwise, for the Controls pill's big
    right-side background watermark. None if the device has no glyph.

    Recoloured to the theme's icon colour (dark in light, light in dark) so it
    reads on both backgrounds. `_dev_pixmap` inverts the black line-art to white
    for the dark UI, which would leave it invisible on the near-white light bg;
    flattening to a single tint is fine here since it's painted at ~0.16 opacity.
    Scoped to the watermark (NOT _dev_pixmap) so the device-picker cards keep
    their full line-art shading."""
    pm = _dev_pixmap(dev, size)
    if pm is None or pm.isNull():
        return pm
    tint = QPixmap(pm.size())
    tint.fill(QColor(0, 0, 0, 0))
    _tp = QPainter(tint)
    _tp.drawPixmap(0, 0, pm)
    _tp.setCompositionMode(QPainter.CompositionMode_SourceAtop)
    _tp.fillRect(tint.rect(), QColor(_c('icon')))
    _tp.end()
    pm = tint
    from PySide6.QtGui import QTransform
    return pm.transformed(QTransform().rotate(angle), Qt.SmoothTransformation)


def _paint_pill_backdrop(btn, e, pm, radius, opacity=0.12, hfrac=0.5, hscale=1.7, vfrac=0.5, fade=False):
    """Paint a big, faint, edge-cropped icon behind a pill button's text - a
    watermark (e.g. the active controller) that doesn't compete with the label.
    Mirrors the source picker's oversized cropped icons. Runs the button's
    normal QSS paint first, then overlays the icon clipped to the rounded rect
    so it's cropped by the borders; child text labels paint on top after.
    hfrac = horizontal centre as a fraction of width (0.5 = middle, lower = left
    side); hscale = icon height as a multiple of the button height."""
    from PySide6.QtWidgets import QPushButton
    QPushButton.paintEvent(btn, e)
    if pm is None or pm.isNull():
        return None
    r = btn.rect()
    p = QPainter(btn)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(r), radius, radius)
    p.setClipPath(path)
    p.setOpacity(opacity)
    sp = pm.scaledToHeight(int(r.height() * hscale), Qt.SmoothTransformation)
    if fade:
        from PySide6.QtGui import QLinearGradient
        _f = QPixmap(sp.size())
        _f.fill(QColor(0, 0, 0, 0))
        _fp = QPainter(_f)
        _fp.drawPixmap(0, 0, sp)
        _fp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        _g = QLinearGradient(0, 0, sp.width(), 0)
        if hfrac < 0.5:
            _g.setColorAt(0.0, QColor(0, 0, 0, 255))
            _g.setColorAt(0.55, QColor(0, 0, 0, 255))
            _g.setColorAt(1.0, QColor(0, 0, 0, 0))
        else:
            _g.setColorAt(0.0, QColor(0, 0, 0, 0))
            _g.setColorAt(0.45, QColor(0, 0, 0, 255))
            _g.setColorAt(1.0, QColor(0, 0, 0, 255))
        _fp.fillRect(_f.rect(), _g)
        _fp.end()
        sp = _f
    p.drawPixmap(int(r.left() + r.width() * hfrac - sp.width() / 2), int(r.top() + r.height() * vfrac - sp.height() / 2), sp)
    p.end()


def _launcher_icon(base, box: int, *, spinner: bool = False, angle=0, dx: int = 0, glow: float = 1.0) -> QIcon:
    """Source icon DIMMED with a launch affordance centred on top: a power glyph
    ('turn it on') or, while launching, a rotating arc. The source art stays
    visible behind it instead of vanishing - the glyph reads as an overlay, not a
    replacement. Built as one pixmap so Qt centres the whole thing where the
    source icon sits (no overlay-vs-icon misalignment to chase)."""
    pm = QPixmap(box, box)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setRenderHint(QPainter.Antialiasing)
    bp = base.pixmap(box, box)
    if not bp.isNull():
        p.setOpacity(0.3)
        p.drawPixmap(0, 0, bp)
        p.setOpacity(1.0)
    if spinner:
        pen = QPen(QColor(_c('icon')), max(2.0, box * 0.09))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        rad = box * 0.3
        c = box / 2.0
        p.drawArc(QRectF(c + dx - rad, c - rad, 2 * rad, 2 * rad), int(-angle * 16), int(-4320))
    else:
        d = max(1, int(box * 0.6))
        off = (box - d) // 2
        _g = max(0.0, min(1.0, glow))
        _v = int(140 + 115 * _g)
        _gl = _kind_icon('power', f'#{_v:02x}{_v:02x}{_v:02x}').pixmap(d, d)
        _sh = _kind_icon('power', '#000000').pixmap(d, d)
        p.setOpacity(0.5)
        p.drawPixmap(off + 1, off + 1, _sh)
        p.setOpacity(0.55 + 0.45 * _g)
        p.drawPixmap(off, off, _gl)
    p.end()
    return QIcon(pm)


def _bulb_pixmap(size: int = 18, color: str = '#ff8a16') -> QPixmap:
    """Lightbulb glyph for the 'Tip' callout cards. Vector so it stays
    crisp at any UI scale."""
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c)
    pen.setWidthF(3.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(18, 8, 28, 28))
    p.drawLine(28, 36, 36, 36)
    p.drawLine(26, 42, 38, 42)
    p.drawLine(27, 48, 37, 48)
    p.drawLine(29, 54, 35, 54)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _trash_pixmap(size: int = 18, color: str = None) -> QPixmap:
    """Simple trash-can glyph for the per-row delete button in the presets
    popup. Drawn vector so it stays crisp at any UI scale."""
    color = color or _c('icon_dim')
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c)
    pen.setWidthF(3.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawLine(14, 18, 50, 18)
    p.drawLine(26, 18, 27, 12)
    p.drawLine(38, 18, 37, 12)
    p.drawLine(27, 12, 37, 12)
    path = QPainterPath()
    path.moveTo(18, 22)
    path.lineTo(21, 52)
    path.lineTo(43, 52)
    path.lineTo(46, 22)
    p.drawPath(path)
    p.drawLine(27, 28, 28, 46)
    p.drawLine(32, 28, 32, 46)
    p.drawLine(37, 28, 36, 46)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _save_pixmap(size: int = 18, color: str = None) -> QPixmap:
    """Floppy-disk 'save' glyph for the Save-current row in the presets popup."""
    color = color or _c('icon_dim')
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c)
    pen.setWidthF(3.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(14, 14)
    path.lineTo(44, 14)
    path.lineTo(50, 20)
    path.lineTo(50, 50)
    path.lineTo(14, 50)
    path.closeSubpath()
    p.drawPath(path)
    p.drawRect(QRectF(22, 36, 20, 12))
    p.drawRect(QRectF(22, 16, 16, 10))
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _globe_pixmap(size: int = 28) -> QPixmap:
    """White globe glyph for the 'Browser' source (YouTube etc.)."""
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(_c('text')))
    pen.setWidthF(3.2)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(7, 7, 50, 50))
    p.drawEllipse(QRectF(23, 7, 18, 50))
    p.drawLine(32, 7, 32, 57)
    p.drawLine(9, 25, 55, 25)
    p.drawLine(9, 39, 55, 39)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _refresh_pixmap(size: int = 16, color: str = None, angle: float = 0.0) -> QPixmap:
    """Circular-arrow 'check for updates' glyph. `angle` spins it (click anim)."""
    color = color or _c('icon_dim')
    import math
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    cx, cy, r = (32, 36, 18)
    if angle:
        p.translate(cx, cy)
        p.rotate(angle)
        p.translate(-cx, -cy)
    pen = QPen(QColor(color))
    pen.setWidthF(6.5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    start_deg, span_deg = (55, 280)
    p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), int(start_deg * 16), int(span_deg * 16))
    end = math.radians(start_deg + span_deg)
    ex, ey = (cx + r * math.cos(end), cy - r * math.sin(end))
    tx, ty = (-math.sin(end), -math.cos(end))
    px, py = (-ty, tx)
    a = 8.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    tri = QPainterPath()
    tri.moveTo(ex + tx * a, ey + ty * a)
    tri.lineTo(ex - tx * 2 + px * a * 0.8, ey - ty * 2 + py * a * 0.8)
    tri.lineTo(ex - tx * 2 - px * a * 0.8, ey - ty * 2 - py * a * 0.8)
    tri.closeSubpath()
    p.drawPath(tri)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _undo_pixmap(size: int = 20, color: str = '#9a9a98') -> QPixmap:
    """Bold 'undo' arrow (matches the chunky reference): a thick shaft running
    LEFT into a big arrowhead, then two 90-degree bends on the tail (right ->
    down -> small left foot). Arrowhead points left (revert / back), distinct
    from the circular refresh glyph."""
    color = color or _c('icon_dim')
    s = 100
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor(color)
    pen = QPen(col)
    pen.setWidthF(11)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(44, 33)
    path.lineTo(64, 33)
    path.quadTo(78, 33, 78, 47)
    path.lineTo(78, 60)
    path.quadTo(78, 73, 65, 73)
    path.lineTo(57, 73)
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    tri = QPainterPath()
    tri.moveTo(14, 33)
    tri.lineTo(42, 16)
    tri.lineTo(42, 50)
    tri.closeSubpath()
    p.drawPath(tri)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _move_pixmap(size: int = 20, color: str = None) -> QPixmap:
    """4-way move glyph: a plus-shaped cross with an arrowhead on each end
    (the universal 'drag to reposition' symbol). Used by the overlay
    'Move on screen' button."""
    color = color or _c('icon_dim')
    s = 100
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor(color)
    cx, cy = (50, 50)
    pen = QPen(col)
    pen.setWidthF(10)
    pen.setCapStyle(Qt.FlatCap)
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawLine(24, cy, 76, cy)
    p.drawLine(cx, 24, cx, 76)
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    h = 14
    for tip, b1, b2 in (((8, cy), (26, cy - h), (26, cy + h)), ((92, cy), (74, cy - h), (74, cy + h)), ((cx, 8), (cx - h, 26), (cx + h, 26)), ((cx, 92), (cx - h, 74), (cx + h, 74))):
        tri = QPainterPath()
        tri.moveTo(*tip)
        tri.lineTo(*b1)
        tri.lineTo(*b2)
        tri.closeSubpath()
        p.drawPath(tri)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _mic_pixmap(size: int = 16, color: str = None) -> QPixmap:
    """Microphone glyph: a rounded capsule body, a U-shaped pickup bracket, a
    stem and a base. Compact label for the 'Duck on my voice' toggle."""
    color = color or _c('icon_dim')
    s = 100
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor(color)
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawRoundedRect(QRectF(38, 16, 24, 46), 12, 12)
    pen = QPen(col)
    pen.setWidthF(7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(28, 30, 44, 44), 2880, 2880)
    p.drawLine(50, 74, 50, 86)
    p.drawLine(38, 88, 62, 88)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _smooth_scroll(area, duration=240):
    """Smooth wheel scrolling: a refresh-rate timer eases the scrollbar toward
    an accumulating target, instead of the old per-notch QPropertyAnimation that
    restarted its OutCubic on every notch (continuous scrolling then felt like a
    string of decelerating hops - chunky / low-fps). Each notch just adds to the
    target; the value chases it at a constant fraction per frame, so a run of
    notches glides as one motion. Wheel-driven widgets (combo / slider / spin)
    under the cursor keep their own wheel behaviour."""
    from PySide6.QtCore import QTimer, QObject, QEvent, Qt
    from PySide6.QtWidgets import QApplication, QComboBox, QAbstractSlider, QAbstractSpinBox
    sb = area.verticalScrollBar()
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
    sb.setCursor(Qt.ClosedHandCursor)
    import math
    refresh = 60.0
    try:
        _sc = area.screen() or QApplication.primaryScreen()
        if _sc is not None and _sc.refreshRate() > 0:
            refresh = float(_sc.refreshRate())
    except Exception:
        pass
    interval = max(3, int(round(1000.0 / refresh)))
    ease = 1.0 - math.exp(-interval / 55.0)
    state = {'target': float(sb.value())}
    timer = QTimer(area)
    timer.setTimerType(Qt.PreciseTimer)
    timer.setInterval(interval)

    def _tick():
        tgt = max(sb.minimum(), min(sb.maximum(), state['target']))
        state['target'] = tgt
        cur = sb.value()
        diff = tgt - cur
        if abs(diff) < 1:
            sb.setValue(int(round(tgt)))
            timer.stop()
            return None
        step = diff * ease
        if abs(step) < 1:
            step = 1.0 if diff > 0 else -1.0
        sb.setValue(int(round(cur + step)))
    timer.timeout.connect(_tick)

    class _Wheel(QObject):
        def eventFilter(self, obj, e):
            if e.type() != QEvent.Wheel:
                return False
            if sb.maximum() <= sb.minimum():
                return False
            w = QApplication.widgetAt(e.globalPosition().toPoint())
            while w is not None and w is not area:
                if isinstance(w, (QComboBox, QAbstractSlider, QAbstractSpinBox)):
                    return False
                w = w.parentWidget()
            d = e.angleDelta().y()
            if d == 0:
                return False
            base = state['target'] if timer.isActive() else float(sb.value())
            state['target'] = max(sb.minimum(), min(sb.maximum(), base - d))
            if not timer.isActive():
                timer.start()
            return True
    flt = _Wheel(area)
    area.viewport().installEventFilter(flt)
    area._smooth_wheel = flt
    area._smooth_timer = timer
    area._smooth_state = state


class _FadeScroll(QScrollArea):
    """Scroll area with a soft top + bottom fade, so content melts into the
    dialog background at the edges. Position-aware: the top fade only shows once
    you've scrolled down, the bottom only while there's more content below - so
    nothing is dimmed at rest."""

    def __init__(self, bg=None, fade_top=14, fade_bot=30, parent=None):
        super().__init__(parent)
        self._bg = QColor(bg if bg is not None else _c('sunk'))
        self._ft = fade_top
        self._fb = fade_bot
        self._top = QWidget(self)
        self._bot = QWidget(self)
        for _w in (self._top, self._bot):
            _w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._top.paintEvent = lambda e: self._paint_fade(self._top, True)
        self._bot.paintEvent = lambda e: self._paint_fade(self._bot, False)
        self.verticalScrollBar().valueChanged.connect(self._update_fades)
        _smooth_scroll(self)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        vw = self.viewport().width()
        self._top.setGeometry(0, 0, vw, _s(self._ft))
        self._bot.setGeometry(0, self.height() - _s(self._fb), vw, _s(self._fb))
        self._top.raise_()
        self._bot.raise_()
        self._update_fades()

    def _update_fades(self):
        sb = self.verticalScrollBar()
        self._top.setVisible(sb.value() > sb.minimum())
        self._bot.setVisible(sb.value() < sb.maximum())

    def _paint_fade(self, w, top):
        p = QPainter(w)
        g = QLinearGradient(0, 0, 0, w.height())
        c_op = QColor(self._bg)
        c_tr = QColor(self._bg)
        c_tr.setAlpha(0)
        g.setColorAt(0.0, c_op if top else c_tr)
        g.setColorAt(1.0, c_tr if top else c_op)
        p.fillRect(w.rect(), g)
        p.end()

    def setWidget(self, w):
        if w is not None:
            name = w.objectName() or 'fadescrollbody'
            w.setObjectName(name)
            w.setStyleSheet((w.styleSheet() or '') + f'\nQWidget#{name}{{background:{self._bg.name()};}}')
        super().setWidget(w)


class _FadeLabel(QLabel):
    """Left-aligned single-line label whose text fades out at the right edge when
    it overflows the widget (instead of a hard clip / ellipsis). Used for the
    update banner headline so it degrades nicely on a narrow window."""

    def __init__(self, text='', fade=34, parent=None):
        super().__init__(text, parent)
        self._fade = fade
        self._color = QColor(_c('text'))

    def setTextColor(self, c):
        self._color = QColor(c)
        self.update()

    def minimumSizeHint(self):
        return QSize(_s(16), self.fontMetrics().height())

    def paintEvent(self, e):
        from PySide6.QtGui import QImage
        txt = self.text()
        flags = int(Qt.AlignLeft | Qt.AlignVCenter)
        avail = self.width()
        if self.fontMetrics().horizontalAdvance(txt) <= avail:
            p = QPainter(self)
            p.setFont(self.font())
            p.setPen(self._color)
            p.drawText(self.rect(), flags, txt)
            return None
        img = QImage(max(1, self.width()), max(1, self.height()), QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        ip = QPainter(img)
        ip.setFont(self.font())
        ip.setPen(self._color)
        ip.drawText(self.rect(), flags, txt)
        f = min(_s(self._fade), avail)
        g = QLinearGradient(avail - f, 0, avail, 0)
        g.setColorAt(0.0, QColor(0, 0, 0, 255))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        ip.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        ip.fillRect(QRectF(avail - f, 0, f, self.height()), g)
        ip.end()
        QPainter(self).drawImage(0, 0, img)


class _VerButton(QPushButton):
    """Version label + refresh icon as ONE clickable, hover-highlighted unit.
    Dim at rest (matches the old version label), brightens on hover; the icon
    spins during a check."""

    def __init__(self, version_text: str):
        super().__init__()
        self.setObjectName('verbar')
        self.setCursor(Qt.PointingHandCursor)
        self.setText(version_text)
        self.setLayoutDirection(Qt.RightToLeft)
        self._dim = _c('verbar_text')
        self._bright = _c('verbar_text_hi')
        self._check = _c('success')
        self._angle = 0.0
        self._mode = 'refresh'
        self._paint_icon(self._dim)

    def _paint_icon(self, color: str) -> None:
        if self._mode == 'check':
            self.setIcon(QIcon(_check_pixmap(15, self._check)))
            return None
        self.setIcon(QIcon(_refresh_pixmap(15, color, self._angle)))

    def set_angle(self, a: float) -> None:
        self._mode = 'refresh'
        self._angle = a
        self._paint_icon(self._bright)

    def show_check(self) -> None:
        """Swap the arrow for a green checkmark (check finished, up to date)."""
        self._mode = 'check'
        self._paint_icon(self._check)

    def show_refresh(self) -> None:
        """Back to the refresh arrow (dim, or bright if hovered)."""
        self._mode = 'refresh'
        self._paint_icon(self._bright if self.underMouse() else self._dim)

    def _retint(self) -> None:
        """Re-read theme colours + repaint the icon (live theme switch); the
        baked _dim/_bright/_check were stale after a switch."""
        self._dim = _c('verbar_text')
        self._bright = _c('verbar_text_hi')
        self._check = _c('success')
        self._paint_icon(self._check if self._mode == 'check' else self._bright if self.underMouse() else self._dim)

    def enterEvent(self, e):
        self._paint_icon(self._bright)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._paint_icon(self._dim)
        super().leaveEvent(e)


def _check_pixmap(size: int = 15, color: str = '#3FB950') -> QPixmap:
    """Checkmark glyph - the version button's 'up to date' state (replaces the
    refresh arrow once a check finds no update)."""
    s = 100
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(13)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(20, 60)
    path.lineTo(42, 80)
    path.lineTo(82, 34)
    p.drawPath(path)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _question_pixmap(size: int = 15, color: str = None) -> QPixmap:
    """Question-mark glyph (Help -> Open guide menu icon)."""
    color = color or _c('icon_dim')
    s = 100
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont('Segoe UI', 74)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(pix.rect(), Qt.AlignCenter, '?')
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _play_pixmap(size: int = 15, color: str = None) -> QPixmap:
    """Filled play triangle (Help -> Replay tour menu icon)."""
    color = color or _c('icon_dim')
    s = 100
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    tri = QPainterPath()
    tri.moveTo(30, 22)
    tri.lineTo(30, 78)
    tri.lineTo(80, 50)
    tri.closeSubpath()
    p.drawPath(tri)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _x_pixmap(size: int = 12, color: str = '#1f1f1e') -> QPixmap:
    """Crisp anti-aliased X (two round-capped diagonals) for the banner close
    button - the "✕" text glyph rendered choppy at small bold sizes."""
    s = 64
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(7)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    m = 19
    p.drawLine(m, m, s - m, s - m)
    p.drawLine(s - m, m, m, s - m)
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _sparkle_pixmap(size: int = 14, color: str = '#1f1f1e') -> QPixmap:
    """Crisp multi-point sparkle (one big + two small 4-point stars, the Tabler
    'sparkles' look) for the update banner's leading glyph. Recolored to the
    banner's contrast text color in _apply_banner_color (like the × close icon)."""
    s = 64
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))

    def star(cx, cy, R, w):
        path = QPainterPath()
        path.moveTo(cx, cy - R)
        path.cubicTo(cx + w, cy - w, cx + w, cy - w, cx + R, cy)
        path.cubicTo(cx + w, cy + w, cx + w, cy + w, cx, cy + R)
        path.cubicTo(cx - w, cy + w, cx - w, cy + w, cx - R, cy)
        path.cubicTo(cx - w, cy - w, cx - w, cy - w, cx, cy - R)
        p.drawPath(path)
    star(s * 0.44, s * 0.5, s * 0.34, s * 0.085)
    star(s * 0.8, s * 0.23, s * 0.16, s * 0.04)
    star(s * 0.23, s * 0.81, s * 0.135, s * 0.034)
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _update_pixmap(size: int = 14, color: str = '#1f1f1e') -> QPixmap:
    """Download/update glyph (down arrow into a tray) for the update banner's
    leading icon. Recolored to the banner's contrast text in _apply_banner_color.
    The sparkle look is kept as _sparkle_pixmap if we want to switch back."""
    s = 64
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(s * 0.105)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    cx = s * 0.5
    p.drawLine(int(cx), int(s * 0.15), int(cx), int(s * 0.55))
    chev = QPainterPath()
    chev.moveTo(cx - s * 0.18, s * 0.37)
    chev.lineTo(cx, s * 0.58)
    chev.lineTo(cx + s * 0.18, s * 0.37)
    p.drawPath(chev)
    tray = QPainterPath()
    tray.moveTo(cx - s * 0.27, s * 0.63)
    tray.lineTo(cx - s * 0.27, s * 0.83)
    tray.lineTo(cx + s * 0.27, s * 0.83)
    tray.lineTo(cx + s * 0.27, s * 0.63)
    p.drawPath(tray)
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


class _BigMenuIconStyle(QProxyStyle):
    """QMenu hard-locks action icons to PM_SmallIconSize (~16px) and they paint
    tiny + edge-hugging (same reason the source picker uses _IconPopup). Override
    that one metric so a QMenu renders its icons at the app scale, crisp - without
    rebuilding the whole nested menu as custom popups."""

    def __init__(self, px: int):
        super().__init__()
        self._px = int(px)

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_SmallIconSize:
            return self._px
        return super().pixelMetric(metric, option, widget)


def _rounded_bordered(src, radius, border='#4d4c47', bw=1.0):
    """Clip an image to a rounded rect + stroke a subtle 1px border - a framed
    preview for the What's-new dialog (so screenshots aren't bare rectangles)."""
    if src is None or src.isNull():
        return src
    out = QPixmap(src.size())
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(bw / 2, bw / 2, src.width() - bw, src.height() - bw)
    clip = QPainterPath()
    clip.addRoundedRect(r, radius, radius)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, src)
    p.setClipping(False)
    pen = QPen(QColor(border))
    pen.setWidthF(bw)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r, radius, radius)
    p.end()
    return out


_CHEV_CACHE = {}


def _chevron_pixmap(size: int, color: str = None) -> QPixmap:
    """Right-pointing chevron '>' for the submenu arrow (Qt's native one is ~7px
    and invisible on dark themes; this scales with the menu font)."""
    color = color or _c('icon_dim')
    s = 64
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawLine(int(s * 0.4), int(s * 0.24), int(s * 0.64), int(s * 0.5))
    p.drawLine(int(s * 0.64), int(s * 0.5), int(s * 0.4), int(s * 0.76))
    p.end()
    return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _chevron_qss_path(size: int, color: str = None) -> str:
    """Paint the chevron to a cached PNG and return a QSS-friendly (forward-slash)
    path, so QMenu::right-arrow can reference it as an image - which Qt then
    right-aligns natively. Empty string on failure (arrow just hides)."""
    color = color or _c('icon_dim')
    key = (int(_s(size)), color)
    cached = _CHEV_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f'segue_chev_{key[0]}_{color.lstrip("#")}.png')
    try:
        _chevron_pixmap(size, color).save(path, 'PNG')
        qss = path.replace('\\', '/')
        _CHEV_CACHE[key] = qss
        return qss
    except Exception:
        return ''


_DOWNCHEV_CACHE = {}


def _down_chevron_qss_path(size: int, color: str = None) -> str:
    """Tinted down-chevron asset -> cached PNG path for QComboBox::down-arrow,
    so every dropdown uses the shipped chevron glyph instead of the native
    Windows arrow. Empty string on failure (combo falls back to no image)."""
    color = color or _c('icon_dim')
    key = (int(_s(size)), color)
    cached = _DOWNCHEV_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    pm = _tinted(os.path.join(_ASSETS, 'down-chevron.png'), color, size)
    if pm is None:
        return ''
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f'segue_downchev_{key[0]}_{color.lstrip("#")}.png')
    try:
        pm.save(path, 'PNG')
        qss = path.replace('\\', '/')
        _DOWNCHEV_CACHE[key] = qss
        return qss
    except Exception:
        return ''


_CHECK_CACHE = {}


def _check_qss_path(size: int, color: str = '#f0f0f0') -> str:
    """Painted check glyph -> cached PNG path for QMenu::indicator:checked. The
    native menu checkmark renders tiny + dim; this is bigger + theme-coloured."""
    key = (int(_s(size)), color)
    cached = _CHECK_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f'segue_check_{key[0]}_{color.lstrip("#")}.png')
    try:
        _check_pixmap(size, color).save(path, 'PNG')
        qss = path.replace('\\', '/')
        _CHECK_CACHE[key] = qss
        return qss
    except Exception:
        return ''


def _badge_html(label: str, bg: str, fg: str) -> str:
    """A rounded pill badge (NEW / FIXED / ...) as an inline <img> data URI, so
    it lives INSIDE the rich text - wrapped lines then start under the badge
    instead of in a separate column. Supersampled x2 for crisp text."""
    import base64
    from PySide6.QtCore import QBuffer, QByteArray
    bf = _ui_font(9, QFont.Bold)
    fm = QFontMetrics(bf)
    img_h = max(1, QFontMetrics(_ui_font(14)).ascent())
    pad_x = _s(6)
    pill_w = fm.horizontalAdvance(label) + 2 * pad_x
    pill_h = min(img_h, fm.height())
    y0 = (img_h - pill_h) / 2.0
    ss = 3
    big = QPixmap(int(pill_w * ss), int(img_h * ss))
    big.fill(QColor(0, 0, 0, 0))
    p = QPainter(big)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(bg))
    _rect = QRectF(0, y0 * ss, pill_w * ss, pill_h * ss)
    _r = _s(4) * ss
    p.drawRoundedRect(_rect, _r, _r)
    fb = QFont(bf)
    if bf.pixelSize() > 0:
        fb.setPixelSize(bf.pixelSize() * ss)
    else:
        fb.setPointSizeF(bf.pointSizeF() * ss)
    p.setFont(fb)
    p.setPen(QColor(fg))
    p.drawText(_rect, Qt.AlignCenter, label)
    p.end()
    pm = big.scaled(int(pill_w), int(img_h), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    pm.save(buf, 'PNG')
    buf.close()
    uri = 'data:image/png;base64,' + base64.b64encode(bytes(ba)).decode('ascii')
    return f"<img src='{uri}' width='{int(pill_w)}' height='{int(img_h)}' style='vertical-align:baseline'>"


def _folder_pixmap(size: int = 28) -> QPixmap:
    """White folder glyph for the 'Local files' source (reads as files, not the
    generic music note)."""
    s = 64
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor(_c('text'))
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    folder = QPainterPath()
    folder.addRoundedRect(QRectF(8, 17, 22, 10), 4, 4)
    folder.addRoundedRect(QRectF(8, 22, 48, 31), 6, 6)
    p.drawPath(folder.simplified())
    p.setPen(QPen(QColor(0, 0, 0, 70), 2))
    p.setBrush(Qt.NoBrush)
    p.drawLine(13, 31, 51, 31)
    p.end()
    return pix.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)


_ASSETS = os.path.join(os.path.dirname(__file__), 'assets')
_CHECK = os.path.join(_ASSETS, 'check.png').replace('\\', '/')
_FORZA = os.path.join(_ASSETS, 'forza.png')
_SPOTIFY = os.path.join(_ASSETS, 'spotify.png')
_LINK = os.path.join(_ASSETS, 'link.png')
_LINK_BROKEN = os.path.join(_ASSETS, 'link_broken.png')
_APP_ICON = os.path.join(_ASSETS, 'segue.png')
_SUPPORT_URL = 'https://ko-fi.com/segueapp'
_DISCORD_URL = 'https://discord.gg/AUrMXdzGZE'


def _load_scaled(path: str, size: int):
    """Load an asset scaled into a size box (keep aspect), or None if missing."""
    if os.path.exists(path):
        pm = QPixmap(path)
        if not pm.isNull():
            return pm.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return None


def _load_icon(path: str, size: int = 20):
    pm = _load_scaled(path, size)
    if pm is not None:
        return QIcon(pm)


def _apply_dwm_titlebar(widget) -> None:
    """Match a native window's title bar to the ACTIVE Segue theme (Windows).
    Dialogs use the native frame, which otherwise follows the SYSTEM dark/light
    setting - so a dark-mode Windows showed dark title bars under Segue's light
    theme. DWMWA_USE_IMMERSIVE_DARK_MODE (20) = 0 light / 1 dark."""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        hwnd = int(widget.winId())
        val = ctypes.byref(ctypes.c_int(0 if _active_theme() == 'light' else 1))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, val, 4)
    except Exception:
        pass


def _round_menu(m) -> None:
    """No-op: menu rounding is now the NATIVE Win11 frame (the QSS sets only the
    fill, no border/radius), so a single corner. Translucent/DONOTROUND hacks
    fought the native rendering and left the double corner; kept as a no-op so
    the call sites stay harmless."""
    pass


def _tinted(path: str, color: str, size: int):
    """Recolor an asset (uses its alpha) to `color` at `size`, or None if absent.
    Lets a single white PNG serve both light (dark bg) and dark (white bg) states."""
    if not os.path.exists(path):
        return None
    src = QPixmap(path)
    if src.isNull():
        return None
    src = src.scaled(_s(size), _s(size), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QPixmap(src.size())
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QColor(color))
    p.end()
    return out


def _media_pixmap(kind: str, color: str, size: int) -> QPixmap:
    """A media glyph: tinted asset (assets/media_<kind>.png) if present, else drawn."""
    t = _tinted(os.path.join(_ASSETS, f'media_{kind}.png'), color, size)
    if t is not None:
        return t
    return _media_icon(kind, color, size).pixmap(QSize(_s(size), _s(size)))


_SENS_HI_THRESH = 0.85
_SENS_LO_THRESH = 0.15


def _sens_to_thresh(sens: int) -> float:
    """Sensitivity slider (0-100) -> Silero vad_threshold. Higher sensitivity =
    lower threshold = ducks more readily."""
    s = max(0, min(100, int(sens)))
    return round(_SENS_HI_THRESH - (_SENS_HI_THRESH - _SENS_LO_THRESH) * s / 100.0, 3)


def _thresh_to_sens(thresh: float) -> int:
    """Inverse of _sens_to_thresh: vad_threshold -> sensitivity 0-100."""
    span = _SENS_HI_THRESH - _SENS_LO_THRESH
    s = (_SENS_HI_THRESH - float(thresh)) / span * 100.0
    return int(round(max(0.0, min(100.0, s))))


_XBOX_BTN = {0: 'A', 1: 'B', 2: 'X', 3: 'Y', 4: 'LB', 5: 'RB', 6: 'View', 7: 'Menu', 8: 'LS', 9: 'RS'}
_XBOX_FACE = {0: 'A', 1: 'B', 2: 'X', 3: 'Y'}
_PS_LABELS = {'cross': '✕', 'circle': '◯', 'square': '▢', 'triangle': '△', 'L1': 'L1', 'R1': 'R1', 'L2': 'L2', 'R2': 'R2', 'L3': 'L3', 'R3': 'R3', 'DpadUp': 'D-pad ↑', 'DpadDown': 'D-pad ↓', 'DpadLeft': 'D-pad ←', 'DpadRight': 'D-pad →', 'micBtn': 'Mic', 'share': 'Share', 'create': 'Create', 'options': 'Options', 'touchpad': 'Touchpad'}


def _pretty_code(code: str, device: str = None) -> str:
    """Human label for a binding code. `device="xbox"` makes btn:N read as
    A/B/X/Y/LB/... instead of the generic "Button N"."""
    if not code:
        return 'Unbound'
    if '+' in code and not code.startswith('key:'):
        return ' + '.join((_pretty_code(p, device) for p in code.split('+')))
    if code.startswith('btn:'):
        if device == 'xbox':
            try:
                return _XBOX_BTN.get(int(code[4:]), 'Button ' + code[4:])
            except ValueError:
                pass
        return 'Button ' + code[4:]
    if code.startswith('hat:'):
        parts = code.split(':')
        return 'D-pad ' + (parts[2].capitalize() if len(parts) > 2 else '')
    if code.startswith('key:'):
        try:
            nums = [int(p) for p in code[4:].split('+') if p != '']
        except ValueError:
            return f'Key {code[4:]}'
        if not nums:
            return 'Unbound'
        mod_names = {17: 'Ctrl', 16: 'Shift', 18: 'Alt'}
        mods = [mod_names[m] for m in (17, 16, 18) if m in nums[:-1]]
        return ' + '.join(mods + [_vk_label(nums[-1])])
    return _PS_LABELS.get(code, code)


def _is_dev() -> bool:
    """Dev-only tools (Demo mode, Preview update banner) appear ONLY when this
    is true - never in shipped builds. Enabled by the env var SEGUE_DEV or a
    marker file %APPDATA%/Segue/dev.flag (persists across reinstalls)."""
    if os.environ.get('SEGUE_DEV'):
        return True
    try:
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.exists(os.path.join(base, 'Segue', 'dev.flag'))
    except Exception:
        return False


_PS_FACE = ('cross', 'circle', 'square', 'triangle')


def _ps_face_pixmap(name: str, px: int, color: str = '#ffffff'):
    """Draw a DualSense face button (outer ring + the shape) at a fixed size so
    all four read the same weight - Unicode glyphs render at wildly different
    sizes (□ tiny, ◯ huge) and have no enclosing ring. Anti-aliased. `color`
    lets it sit on light buttons (dark glyph) as well as dark ones."""
    if name not in _PS_FACE:
        return None
    from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QPolygonF
    from PySide6.QtCore import QRectF, QPointF
    px = int(px)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.5, px * 0.075))
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    om = px * 0.06
    p.drawEllipse(QRectF(om, om, px - 2 * om, px - 2 * om))
    im = px * 0.33
    w = px - 2 * im
    cx = px / 2
    if name == 'circle':
        cm = px * 0.25
        p.drawEllipse(QRectF(cm, cm, px - 2 * cm, px - 2 * cm))
    elif name == 'square':
        p.drawRect(QRectF(im, im, w, w))
    elif name == 'triangle':
        h = px * 0.42
        a = px / 2 - 2 * h / 3
        bw = px * 0.25
        p.drawPolygon(QPolygonF([QPointF(cx, a), QPointF(cx + bw, a + h), QPointF(cx - bw, a + h)]))
    else:
        p.drawLine(QPointF(im, im), QPointF(px - im, px - im))
        p.drawLine(QPointF(px - im, im), QPointF(im, px - im))
    p.end()
    return pm


def _xbox_face_pixmap(letter: str, px: int, color: str = '#ffffff'):
    """Draw an Xbox face button: outer ring (same weight as the DualSense glyphs)
    + the letter A/B/X/Y centered. Monochrome to match the PS glyph style - the
    iconic green/red/blue/yellow would clash with Segue's tonal UI."""
    from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QFont, QFontMetrics
    from PySide6.QtCore import QRectF, QPointF
    px = int(px)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.5, px * 0.075))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    om = px * 0.06
    p.drawEllipse(QRectF(om, om, px - 2 * om, px - 2 * om))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(max(7, int(round(px * 0.5))))
    p.setFont(f)
    br = QFontMetrics(f).tightBoundingRect(letter)
    bx = (px - br.width()) / 2.0 - br.left()
    by = (px - br.height()) / 2.0 - br.top()
    p.drawText(QPointF(bx, by), letter)
    p.end()
    return pm


def _fetch_pixmap(url: str, max_w: int):
    """Best-effort fetch of a remote image -> QPixmap scaled to max_w. Short
    timeout; returns None on any failure (so the What's new dialog still works
    without an image). Called only when a release sets image_url."""
    try:
        if url.lower().startswith(('http://', 'https://')):
            import urllib.request
            req = urllib.request.Request(url, headers={'User-Agent': 'Segue'})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = r.read()
            pm = QPixmap()
            pm.loadFromData(data)
        else:
            pm = QPixmap(url)
        if pm.isNull():
            return None
        if pm.width() > max_w:
            pm = pm.scaledToWidth(int(max_w), Qt.SmoothTransformation)
        return pm
    except Exception:
        return None


def bundled_whatsnew_version() -> str:
    """Version of the bundled What's-new CONTENT (assets/whatsnew.json). Drives the
    post-update popup, decoupled from the app VERSION: a silent patch keeps the same
    content version (no re-pop), a notable release bumps it (shows). '' if absent."""
    import json
    try:
        with open(os.path.join(_ASSETS, 'whatsnew.json'), encoding='utf-8') as f:
            return str(json.load(f).get('version', '')).strip()
    except Exception:
        return ''


def _theme_note_html(html: str) -> str:
    """Release-note markup bakes white emphasis (color:#ffffff) for dark mode.
    Re-point hardcoded white to the theme's primary text so titles read in light
    mode too (dark theme's text is ~white, so it looks unchanged there)."""
    import re
    return re.sub('color:\\s*#?(?:ffffff|fff|white)\\b', f'color:{_c("text")}', html, flags=re.IGNORECASE)


def _localize_inline_images(html: str) -> str:
    """Inline every <img> in What's-new notes as a base64 data URI.

    Two reasons: (1) QLabel rich text never downloads remote images, so hosted
    https sources must be fetched ourselves; (2) Qt's rich-text pixmap cache
    COLLIDES on same-sized images loaded from different file paths - two 380px
    screenshots rendered as the same image (verified with an isolated probe).
    Data URIs are immune to both. Downloads cache to temp by URL hash so
    re-opening the dialog is instant; a failed fetch drops just that image."""
    import re, io, hashlib, tempfile, urllib.request, base64
    from PIL import Image

    def repl(m):
        tag, src = m.group(0), m.group('src')
        if src.lower().startswith('data:'):
            return tag
        wm = re.search("width='(\\d+)'", tag)
        w = int(wm.group(1)) if wm else None
        try:
            if src.lower().startswith(('http://', 'https://')):
                path = os.path.join(tempfile.gettempdir(), 'segue_wn_' + hashlib.md5(src.encode()).hexdigest() + '.png')
                if not os.path.exists(path):
                    req = urllib.request.Request(src, headers={'User-Agent': 'Segue'})
                    with urllib.request.urlopen(req, timeout=6) as r:
                        data = r.read()
                    im = Image.open(io.BytesIO(data)).convert('RGBA')
                    bbox = im.getbbox()
                    if bbox:
                        im = im.crop(bbox)
                    if w and im.width != w:
                        im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
                    im.save(path)
                mime = 'image/png'
            else:
                path = src
                if w:
                    im = Image.open(path)
                    if im.width != w:
                        im = im.convert('RGBA').resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
                        buf = io.BytesIO()
                        im.save(buf, 'PNG')
                        b = base64.b64encode(buf.getvalue()).decode()
                        return f"<img src='data:image/png;base64,{b}'>"
                mime = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
            with open(path, 'rb') as f:
                b = base64.b64encode(f.read()).decode()
            return f"<img src='data:{mime};base64,{b}'>"
        except Exception:
            return ''
    return re.sub("<img[^>]*src='(?P<src>[^']+)'[^>]*>", repl, html)


def _contrast_text(hex_color: str) -> str:
    """Dark or light text, whichever reads on the given background hex."""
    try:
        h = hex_color.lstrip('#')
        if len(h) == 3:
            h = ''.join((c * 2 for c in h))
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        if lum > 0.55:
            return '#1f1f1e'
        return '#f0f0f0'
    except Exception:
        return '#1f1f1e'


def _combo_pixmap(code: str, px: int, color: str = '#ffffff', device: str = None):
    """Composite a controller combo ("L1+square", "btn:4+btn:2") into one pixmap:
    each part as its drawn face glyph (cross/circle/square/triangle, same ring as
    a single bind) or its text label, separated by " + ". Returns None for a
    non-combo or key code so callers fall back to single-bind rendering. This is
    why a combo's face buttons read at full size instead of as a tiny Unicode
    glyph from the text path."""
    if not code or '+' not in code or code.startswith('key:'):
        return None
    from PySide6.QtGui import QPixmap, QPainter, QColor, QFontMetrics
    from PySide6.QtCore import Qt, QRectF
    parts = [p for p in code.split('+') if p]
    if len(parts) < 2:
        return None
    font = _ui_font(int(round(px * 0.62)), QFont.Bold)
    fm = QFontMetrics(font)
    sep = '  +  '
    sep_w = fm.horizontalAdvance(sep)
    segs = []
    for p in parts:
        face = _ps_face_pixmap(p, px, color)
        if face is None and device == 'xbox' and p.startswith('btn:'):
            try:
                _n = int(p[4:])
                if _n in _XBOX_FACE:
                    face = _xbox_face_pixmap(_XBOX_FACE[_n], px, color)
            except ValueError:
                pass
        if face is not None:
            segs.append(('pm', face, face.width(), face.height()))
            continue
        t = _pretty_code(p, device)
        segs.append(('txt', t, fm.horizontalAdvance(t), px))
    total = max(1, sum((w for _, _, w, _ in segs)) + sep_w * (len(segs) - 1))
    pm = QPixmap(total, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setFont(font)
    p.setPen(QColor(color))
    x = 0.0
    for i, (kind, payload, w, h) in enumerate(segs):
        if i > 0:
            p.drawText(QRectF(x, 0, sep_w, px), Qt.AlignCenter, sep)
            x += sep_w
        if kind == 'pm':
            p.drawPixmap(int(round(x)), int(round((px - h) / 2)), payload)
        else:
            p.drawText(QRectF(x, 0, w, px), Qt.AlignVCenter | Qt.AlignLeft, payload)
        x += w
    p.end()
    return pm


def _set_bind_visual(w, code, fallback: str = '', px: int = 22, suffix: str = '', device: str = None):
    """Render a bind on a keycap button or a QLabel: a drawn icon for the four
    DualSense face buttons (uniform size + ring) and composited combos, plain
    text for everything else. Falls back to `fallback` for an empty code.
    `suffix` rides inside the cap after the bind name, e.g. "(hold)" on the Open
    Segue row."""
    from PySide6.QtGui import QIcon, QPixmap
    try:
        pm = _combo_pixmap(code, px, _c('text'), device)
    except Exception:
        pm = None
    if pm is None and device == 'xbox' and code and code.startswith('btn:'):
        try:
            _n = int(code[4:])
            if _n in _XBOX_FACE:
                pm = _xbox_face_pixmap(_XBOX_FACE[_n], px, _c('text'))
        except ValueError:
            pm = None
    if pm is None:
        pm = _ps_face_pixmap(code, px, _c('text'))
    is_btn = isinstance(w, QAbstractButton)
    if pm is not None:
        if is_btn:
            w.setText(suffix)
            w.setIcon(QIcon(pm))
            w.setIconSize(pm.size())
            return None
        w.setText(suffix)
        w.setPixmap(pm)
        return None
    txt = _pretty_code(code, device) if code else (fallback or 'Unbound')
    if suffix:
        txt = f'{txt} {suffix}'
    if is_btn:
        w.setIcon(QIcon())
        w.setText(txt)
        return None
    w.setPixmap(QPixmap())
    w.setText(txt)


def _vk_label(vk: int) -> str:
    """Human label for a single Windows virtual-key code."""
    named = {8: 'Backspace', 9: 'Tab', 13: 'Enter', 16: 'Shift', 17: 'Ctrl', 18: 'Alt', 19: 'Pause', 20: 'Caps Lock', 27: 'Esc', 32: 'Space', 33: 'Page Up', 34: 'Page Down', 35: 'End', 36: 'Home', 37: 'Left', 38: 'Up', 39: 'Right', 40: 'Down', 45: 'Insert', 46: 'Delete', 106: 'Numpad *', 107: 'Numpad +', 109: 'Numpad -', 110: 'Numpad .', 111: 'Numpad /'}
    if vk in named:
        return named[vk]
    if 96 <= vk <= 105:
        return f'Numpad {vk - 96}'
    if 112 <= vk <= 123:
        return f'F{vk - 111}'
    try:
        import ctypes
        ch = ctypes.windll.user32.MapVirtualKeyW(int(vk), 2) & 65535
        if ch >= 32 and chr(ch).strip():
            return chr(ch).upper()
    except Exception:
        pass
    if 48 <= vk <= 90:
        return chr(vk)
    return f'Key {vk}'


def _build_qss(check: str) -> str:
    """Stylesheet with every px scaled by _s() for live UI scaling."""
    hw = _s(18)
    bw = max(1, _s(2))
    gh = _s(6)
    hrad = (hw + 2 * bw) // 2
    hmar = (hw + 2 * bw - gh) // 2
    chev = _chevron_qss_path(13)
    chk = _check_qss_path(14, _c('text'))
    cbchk = _check_qss_path(14, _c('emph_text'))
    downchev = _down_chevron_qss_path(12)
    _bb = f'1px solid {_c("border")}' if _active_theme() == 'light' else 'none'
    _tog_act_bd = f'1px solid {_c("emph_fill")}' if _active_theme() == 'light' else 'none'
    _pwr_act_bd = f'1px solid {_ACCENT}' if _active_theme() == 'light' else 'none'
    _tab_act_bd = f'1px solid {_c("panel")}' if _active_theme() == 'light' else 'none'
    return f"""
* {{ color: {_c('text')}; }}
QLabel#hint {{ color: {_c('text_hint')}; }}
QLabel:disabled, QLabel#hint:disabled {{ color: {_c('text_disabled')}; }}   /* greyed when row off */
QCheckBox::indicator {{ width: {_s(22)}px; height: {_s(22)}px; border-radius: {_s(6)}px;
    border: 1px solid {_c('border_hi')}; background: {_c('surface')}; }}
QCheckBox::indicator:checked {{ background: {_c('emph_fill')}; border: 1px solid {_c('emph_fill')};
    image: url("{cbchk}"); }}
QCheckBox:disabled {{ color: {_c('text_disabled')}; }}
QCheckBox::indicator:disabled {{ background: {_c('sunk')}; border: 1px solid {_c('border')}; }}
QCheckBox::indicator:checked:disabled {{ background: {_c('text_disabled')}; border: 1px solid {_c('text_disabled')}; }}
QSlider:horizontal {{ min-height: {_s(24)}px; }}
QSlider::groove:horizontal {{ height: {gh}px; background: {_c('border')}; border-radius: {gh // 2}px; }}
QSlider::sub-page:horizontal {{ background: {_c('emph_fill')}; border-radius: {gh // 2}px; }}
QSlider::add-page:horizontal {{ background: {_c('border')}; border-radius: {gh // 2}px; }}
QSlider::handle:horizontal {{ width: {hw}px; height: {hw}px; margin: -{hmar}px 0;
    background: {_c('emph_fill')}; border: {bw}px solid {_c('panel')}; border-radius: {hrad}px; }}
/* Hover keeps a CONTRASTING outline (border_hi went white in high-contrast =
   same as the white handle/track -> no separation). emph_text is defined as the
   contrast to emph_fill, so the handle stays separated from the filled track. */
QSlider::handle:horizontal:hover {{ border-color: {_c('emph_text')}; }}
QSlider::handle:horizontal:pressed {{ background: {_c('emph_fill')}; }}
QSlider::groove:horizontal:disabled {{ background: {_c('surface')}; }}
QSlider::sub-page:horizontal:disabled {{ background: {_c('border_hi')}; }}
QSlider::handle:horizontal:disabled {{ background: {_c('text_disabled')}; border-color: {_c('panel')}; }}
QPushButton {{ background: {_c('surface')}; border: none; border-radius: {_s(8)}px;
    padding: {_s(7)}px {_s(12)}px; }}
QPushButton:hover {{ background: {_c('surface_hi')}; }}
QPushButton#togglebtn {{ background: {_c('surface')}; border: {_bb}; border-radius: {_s(8)}px;
    padding: {_s(9)}px {_s(10)}px; min-height: {_s(20)}px; }}
QPushButton#togglebtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#togglebtn[active="true"] {{ background: {_c('emph_fill')}; color: {_c('emph_text')}; border: {_tog_act_bd}; }}
QPushButton#togglebtn:disabled {{ color: {_c('text_disabled')}; }}
/* Skip-input pills (D-pad / Touchpad swipe) + pause Tap/Press: styled like the
   bind key-caps - outlined, not a solid fill. Active = orange outline + white
   text on a faint orange tint (matches keycap hover), so the chosen option
   reads as "engaged" the same way a bound cap does. 2px border on every state
   so toggling active doesn't shift the layout. */
QPushButton#skipbtn {{ background: {_c('deep')}; border: 2px solid {_c('border_hi')}; border-radius: {_s(8)}px;
    padding: {_s(7)}px {_s(14)}px; min-height: {_s(20)}px; font-weight: 600; color: {_c('text')}; }}
QPushButton#skipbtn:hover {{ border-color: {_ACCENT}; background: {_c('accent_tint')}; }}
QPushButton#skipbtn[active="true"] {{ background: {_c('accent_tint')}; border: 2px solid {_ACCENT}; color: {_c('text')}; }}
QPushButton#skipbtn[active="true"]:hover {{ border-color: {_c('accent_hi')}; }}
QPushButton#powerbtn {{ background: {_c('surface')}; border: {_bb}; border-radius: {_s(8)}px;
    padding: {_s(9)}px {_s(10)}px; min-height: {_s(20)}px; }}
QPushButton#powerbtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#powerbtn[active="true"] {{ background: {_ACCENT}; color: {_c('emph_text')}; border: {_pwr_act_bd}; }}
/* The 5 row buttons (media + tabs) share one height via min/max-height so they
   match exactly; the active tab is the only one made taller (to meet its panel). */
QPushButton#mediabtn {{ background: {_c('surface')}; border: {_bb}; border-radius: {_s(8)}px;
    padding: {_s(6)}px {_s(10)}px; min-height: {_s(32)}px; max-height: {_s(32)}px; }}
QPushButton#mediabtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#playbtn {{ background: {_c('btn_fill')}; border: {_bb}; border-radius: {_s(8)}px;
    padding: {_s(6)}px {_s(10)}px; min-height: {_s(32)}px; max-height: {_s(32)}px; }}
QPushButton#playbtn:hover {{ background: {_c('btn_fill_hi')}; }}
QPushButton#playbtn[playing="true"] {{ background: {_c('btn_dull')}; }}   /* duller while playing */
QPushButton#playbtn[playing="true"]:hover {{ background: {_c('btn_dull_hi')}; }}
QPushButton#tabbtn {{ background: {_c('surface')}; border: {_bb}; border-radius: {_s(8)}px;
    padding: {_s(6)}px {_s(10)}px; min-height: {_s(32)}px; max-height: {_s(32)}px; }}
QPushButton#tabbtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#tabbtn[dull="true"] {{ background: {_c('surface_dull')}; }}
QPushButton#tabbtn[dull="true"]:hover {{ background: {_c('surface_hi')}; }}
QPushButton#tabbtn[active="true"] {{ background: {_c('panel')}; border: {_tab_act_bd};
    /* fill-coloured (invisible) border keeps the SAME 1px box as the outlined
       rest tab so the icon doesn't jump on open; it still merges into its panel */
    /* same content height as inactive; the +7 to reach the panel is bottom padding
       so the icon stays put instead of recentering downward */
    min-height: {_s(32)}px; max-height: {_s(32)}px;
    padding: {_s(6)}px {_s(10)}px {_s(13)}px {_s(10)}px;
    border-top-left-radius: {_s(8)}px; border-top-right-radius: {_s(8)}px;
    border-bottom-left-radius: 0; border-bottom-right-radius: 0; }}
QPushButton#tabbtn[active="true"]:hover {{ background: {_c('panel')}; }}
QFrame#tabpanel {{ background: {_c('panel')}; border: none; border-radius: {_s(8)}px; }}
QFrame#tabpanelL {{ background: {_c('panel')}; border: none; border-radius: {_s(8)}px;
    border-top-left-radius: 0; }}   /* Mixer panel: sharp top-left (tab sits at the edge) */
QPushButton#advbtn {{ background: transparent; border: none; text-align: left;
    color: {_c('text_hint')}; padding: {_s(2)}px {_s(2)}px; }}
QPushButton#advbtn:hover {{ color: {_c('text')}; }}
QPushButton#bindsbtn::menu-indicator {{ image: none; width: 0; }}
QPushButton#devbtn {{ background: {_c('surface')}; border: 2px solid transparent;
    border-radius: {_s(6)}px; padding: {_s(5)}px {_s(8)}px; }}
QPushButton#devbtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#devbtn:checked {{ background: {_c('surface')}; border: 2px solid {_ACCENT}; }}
QToolButton#devcard {{ background: {_c('surface')}; border: 2px solid transparent;
    border-radius: {_s(10)}px; color: {_c('text')}; padding: {_s(10)}px; }}
QToolButton#devcard:hover {{ background: {_c('surface_hi')}; border: 2px solid {_ACCENT}; }}
/* Save uses emph_fill (dark slab in light, white in dark) - btn_fill is the
   BRIGHT white play pill, which on the light dialog was white-on-near-white and
   vanished (worse on hover). emph keeps the primary action high-contrast in both
   themes. 1px transparent border = same box as the disabled border (no shift).
   Disabled gets a visible surface fill + border so the button still reads as a
   button when greyed (sunk was invisible against the dialog). */
QPushButton#savebtn {{ background: {_c('emph_fill')}; color: {_c('emph_text')};
    border: 1px solid transparent; border-radius: {_s(8)}px;
    padding: {_s(9)}px {_s(12)}px; font-weight: 700; }}
QPushButton#savebtn:hover {{ background: {_c('emph_fill_hi')}; }}
QPushButton#savebtn:disabled {{ background: {_c('surface')}; color: {_c('text_disabled')};
    border: 1px solid {_c('border')}; }}
QFrame#card {{ background: {_c('surface')}; border: {_bb}; border-radius: {_s(8)}px; }}
QFrame#vline {{ background: {_c('border')}; border: none; }}
QLabel#nptitle {{ color: {_c('text')}; }}
QWidget#titlebar {{ background: {_c('titlebar')}; }}
/* Titlebar buttons: square hover fill (border-radius 0) like a native window
   caption, NOT the global rounded-button look they were inheriting from the
   generic QPushButton rule. The window's own rounded corner is DWM-clipped,
   so the close button's top-right follows it automatically. */
QPushButton#menubtn {{ background: transparent; border: none; border-radius: 0; }}
QPushButton#menubtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#menubtn::menu-indicator {{ image: none; width: 0; }}
/* Version + refresh = one rounded, contained hover unit (not a tall square). */
QPushButton#verbar {{ background: transparent; border: none; border-radius: 7px;
                      color: {_c('verbar_text')}; padding: 3px 8px; text-align: left; }}
QPushButton#verbar:hover {{ background: {_c('surface_hi')}; color: {_c('verbar_text_hi')}; }}
/* Flat icon-only undo button (overlay-size reset): no box, subtle hover. */
QPushButton#undobtn {{ background: transparent; border: none; border-radius: {_s(6)}px;
                       padding: {_s(4)}px; }}
QPushButton#undobtn:hover {{ background: {_c('surface_hi')}; }}
QPushButton#capbtn, QPushButton#capclose {{ background: transparent; border: none; border-radius: 0; }}
QPushButton#capbtn:hover {{ background: {_c('surface_hi')}; }}
/* Close hover is the universal Windows close-red in EVERY theme (the contrast
   theme's danger is a light salmon that read weak as a close button); the X
   glyph flips to white on hover via enter/leaveEvent. */
QPushButton#capclose:hover {{ background: #c42b1c; }}
/* Tooltips: without an explicit rule Qt falls back to the OS tooltip palette
   (light on Win10/11), and the app's light text then renders white-on-white /
   unreadable. Pin a themed tooltip everywhere so it always matches. */
QToolTip {{ background-color: {_c('panel')}; color: {_c('text')}; border: 1px solid {_c('border')};
    border-radius: {_s(5)}px; padding: {_s(4)}px {_s(7)}px; }}
/* No QSS border/border-radius on the menu FRAME: the native Win11 style rounds
   the menu window itself, and a QSS rounded rect on top sat at a different
   radius -> a second, inset rounded border ('double corner'). Just recolour the
   fill; the native frame is the single rounded corner. */
QMenu {{ background: {_c('panel')}; border: none; padding: {_s(6)}px; }}
QMenu::item {{ padding: {_s(7)}px {_s(22)}px {_s(7)}px {_s(10)}px; border-radius: {_s(6)}px; }}
/* Icons hug the popup's left edge by default; pad them inward so they sit
   balanced inside the rounded item highlight (Help submenu glyphs). */
QMenu::icon {{ padding-left: {_s(10)}px; }}
QMenu::item:!selected {{ background: transparent; }}
QMenu::item:selected {{ background: {_c('surface_hi')}; }}
/* QWidgetAction rows (Uninstall / Quit) with the icon pinned right - replicate
   the item hover highlight since they aren't native QMenu::items. */
QWidget#menurowR {{ background: transparent; border-radius: {_s(6)}px; }}
QWidget#menurowR:hover {{ background: {_c('surface_hi')}; }}
QLabel#menurowtext {{ color: {_c('text')}; background: transparent; }}
/* Update banner: slim orange bar under the titlebar. Hidden until a new
   release lands, then shown with a Get-it button + small × dismiss. */
QWidget#updatebanner {{ background: {_ACCENT}; }}
QLabel#updatebannertext {{ color: #1f1f1e; padding-left: {_s(2)}px; }}
QPushButton#updatebannerbtn {{ background: #1f1f1e; color: {_ACCENT};
    border: none; border-radius: {_s(6)}px; padding: {_s(4)}px {_s(12)}px;
    min-height: 0; }}
QPushButton#updatebannerbtn:hover {{ background: #2b2b29; }}
QPushButton#updatebannerclose {{ background: transparent; color: #1f1f1e;
    border: none; padding: 0; }}
QPushButton#updatebannerclose:hover {{ background: rgba(0,0,0,0.12);
    border-radius: {_s(12)}px; }}
QPushButton#updatebannerlink {{ background: transparent; color: #1f1f1e;
    border: none; padding: 0 {_s(6)}px; font-weight: 600; text-decoration: underline; }}
QPushButton#updatebannerlink:hover {{ color: #000000; }}
QPushButton#resetbtn {{ background: {_c('surface')}; border: 1px solid {_c('border')}; color: {_c('text_dim')};
    border-radius: {_s(6)}px; padding: {_s(3)}px {_s(10)}px; min-height: 0; }}
QPushButton#resetbtn:hover {{ background: {_c('surface_hi')}; border-color: {_c('border_hi')}; color: {_c('text')}; }}
QPushButton#srcbtn {{ background: transparent; border: none; color: {_c('text_dim')};
    padding: {_s(6)}px {_s(8)}px; }}
QPushButton#srcbtn:hover {{ background: {_c('surface_hi')}; border-radius: {_s(6)}px; }}
/* Source split: the pill bg is painted by the container (_paint_src_box) so the
   inner edges at the gap stay SHARP (Qt's per-corner QSS radius rounds all four).
   The segment buttons are transparent click/icon targets. */
QFrame#srcbox {{ background: transparent; }}
QPushButton#srcseg {{ background: transparent; border: none; color: {_c('text_dim')}; }}
/* Custom icon-popup (source picker etc.) - replaces QMenu so the icons
   can scale freely. Rounded frame, hover-lit rows, left-aligned. */
QFrame#iconpopup {{ background: {_c('panel')}; border: 1px solid {_c('border')};
    border-radius: {_s(8)}px; }}
/* 'Tip' callout card in the Setup tab - tinted box, accent left edge. */
QFrame#tipcard {{ background: {_c('accent_tint')}; border: 1px solid {_c('border')};
    border-left: {_s(3)}px solid {_ACCENT}; border-radius: {_s(8)}px; }}
QPushButton#popupitem {{ background: transparent; border: none; color: {_c('text')};
    text-align: left; padding: {_s(8)}px {_s(12)}px; border-radius: {_s(6)}px; }}
QPushButton#popupitem:hover {{ background: {_c('surface_hi')}; }}
/* Per-row delete button in the presets popup. Trash icon stays dim until
   hovered, then lights red so deletion reads as a deliberate action. */
QPushButton#presetdel {{ background: transparent; border: none;
    border-radius: {_s(6)}px; padding: {_s(8)}px {_s(6)}px; }}
QPushButton#presetdel:hover {{ background: {_c('danger_tint')}; }}
QComboBox {{ background: {_c('surface')}; color: {_c('text')}; border: 1px solid {_c('border')};
    border-radius: {_s(4)}px; padding: {_s(4)}px {_s(8)}px; }}
QComboBox:hover {{ background: {_c('surface_hi')}; }}
/* Disabled combo (e.g. mic picker when Speech recognition / Include self is off):
   without this it keeps full colour and looks active. Dim it like the rest. */
QComboBox:disabled {{ background: {_c('sunk')}; color: {_c('text_disabled')}; border-color: {_c('border')}; }}
QComboBox::drop-down {{ border: none; width: {_s(18)}px; }}
QComboBox::down-arrow {{ image: url("{downchev}"); width: {_s(12)}px; height: {_s(12)}px; }}
/* Subtle grey row highlight (selection-background-color), NOT an accent-orange
   ::item bleed - the orange + taller item padding read heavy in the mic picker
   ("we had it good"). Back to the shipped subtle look. */
QComboBox QAbstractItemView {{ background: {_c('panel')}; color: {_c('text')};
    border: 1px solid {_c('border')}; outline: none;
    selection-background-color: {_c('surface_hi')}; padding: {_s(4)}px; }}
QMenu::separator {{ height: 1px; background: {_c('border')}; margin: {_s(5)}px {_s(8)}px; }}
/* Submenu chevron: a painted PNG (Qt's built-in is ~7px + invisible on dark)
   set as the right-arrow IMAGE so Qt right-aligns it natively at the menu's
   right edge, sized to the menu font. Replaces the old "  >" text in titles. */
QMenu::right-arrow {{ image: url("{chev}"); width: {_s(13)}px; height: {_s(13)}px;
    subcontrol-position: center right; right: {_s(8)}px; }}
/* Checked-item tick (View / scale menu): bigger + theme-coloured, nudged in
   toward the label so it isn't stranded at the far left. */
QMenu::indicator {{ width: {_s(14)}px; height: {_s(14)}px; left: {_s(6)}px; }}
QMenu::indicator:checked {{ image: url("{chk}"); }}
QMenu::indicator:non-exclusive:checked {{ image: url("{chk}"); }}
QMenu::indicator:exclusive:checked {{ image: url("{chk}"); }}
QMenu::indicator:unchecked {{ image: none; }}
QMessageBox {{ background: {_c('sunk')}; }}
QMessageBox QLabel {{ color: {_c('text')}; }}
QInputDialog, QDialog {{ background: {_c('sunk')}; }}
QInputDialog QLabel, QDialog QLabel {{ color: {_c('text')}; }}
QLineEdit {{ background: {_c('surface')}; color: {_c('text')}; border: 1px solid {_c('border')};
    border-radius: {_s(6)}px; padding: {_s(5)}px {_s(8)}px; selection-background-color: {_c('border_hi')}; }}
/* Generic list (the "Pick app" picker). WITHOUT an explicit rule an unstyled
   QListWidget falls back to the SYSTEM palette - so on a Windows-dark-mode
   machine the list body rendered dark even in Segue's light theme. Theme it. */
QListWidget {{ background: {_c('surface')}; color: {_c('text')};
    border: 1px solid {_c('border')}; border-radius: {_s(6)}px; outline: none; padding: {_s(3)}px; }}
QListWidget::item {{ padding: {_s(7)}px {_s(9)}px; border-radius: {_s(4)}px; }}
QListWidget::item:hover {{ background: {_c('surface_hi')}; }}
QListWidget::item:selected {{ background: {_ACCENT}; color: {_c('emph_text')}; }}
QListWidget#helpnav {{ background: {_c('panel')}; border: none; border-radius: {_s(8)}px;
    padding: {_s(6)}px; outline: none; }}
QListWidget#helpnav::item {{ color: {_c('text_dim')}; padding: {_s(8)}px {_s(10)}px;
    border-radius: {_s(6)}px; margin-bottom: {_s(2)}px; }}
QListWidget#helpnav::item:hover {{ background: {_c('surface_hi')}; color: {_c('text')}; }}
QListWidget#helpnav::item:selected {{ background: {_ACCENT}; color: {_c('emph_text')}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
/* Thin track LINE + a rounded HANDLE over it. Page-margins squeeze the
   add/sub-page into a ~2px centred line; the handle's smaller margins make it an
   8px rounded bar (radius 4 = full pill, still thin). Hover brightens. Fixed px
   (not _s): _s rounding kept flattening the handle's corners. Verified offscreen. */
QScrollBar:vertical {{ background: transparent; width: 16px; margin: 5px 0; }}
QScrollBar::handle:vertical {{ background: {_c('scrollbar')}; border-radius: 4px;
    min-height: 40px; margin: 0 4px; }}
QScrollBar::handle:vertical:hover {{ background: {_c('scrollbar_hi')}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; width: 0; background: none; border: none; }}
QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {{ width: 0; height: 0; background: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: {_c('border')}; margin: 0 7px; border-radius: 2px; }}
"""


class _Card(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName('card')


def _rounded_cover(data: bytes, size: int, radius: int) -> QPixmap:
    """Decode cover-art bytes into a rounded square pixmap."""
    src = QPixmap()
    src.loadFromData(data)
    src = src.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, src)
    p.end()
    return out


def _cover_placeholder(size: int, radius: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_c('sunk')))
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.end()
    return pm


def _friendly_app(appid: str) -> str:
    """Map an SMTC source app-id to a friendly browser/player name, or '' if
    unknown. Lets the now-playing subtitle show 'Firefox' / 'Chrome' etc. when a
    page provides no artist."""
    a = (appid or '').lower()
    if not a:
        return ''
    for key, name in (('308046b0af4a39cb', 'Firefox'), ('firefox', 'Firefox'), ('mozilla', 'Firefox'), ('msedge', 'Edge'), ('chromium', 'Chrome'), ('chrome', 'Chrome'), ('brave', 'Brave'), ('opera', 'Opera'), ('vivaldi', 'Vivaldi'), ('yandex', 'Yandex'), ('spotify', 'Spotify'), ('zune', 'Media Player'), ('vlc', 'VLC'), ('applemusic', 'Apple Music'), ('apple music', 'Apple Music'), ('tidal', 'TIDAL'), ('amazon', 'Amazon Music'), ('youtube music', 'YouTube Music'), ('youtube-music', 'YouTube Music'), ('th-ch', 'YouTube Music'), ('ytmd', 'YouTube Music')):
        if key in a:
            return name
    return ''


_BROWSER_EXE = {
    'Firefox': ('firefox.exe',),
    'Chrome': ('chrome.exe',),
    'Edge': ('msedge.exe',),
    'Opera': ('opera.exe', 'operagx.exe'),
    'Brave': ('brave.exe',),
    'Vivaldi': ('vivaldi.exe',),
    'Yandex': ('yandex.exe',),
}


def _find_browser_exe(friendly: str):
    """Path to the running browser's exe (for its icon), or None. psutil is lazy
    imported + this is only called once per browser until cached, so the process
    scan is cheap in practice."""
    names = _BROWSER_EXE.get(friendly, ())
    if not names:
        return None
    nl = {n.lower() for n in names}
    try:
        import psutil
        for p in psutil.process_iter(['name', 'exe']):
            try:
                if (p.info.get('name') or '').lower() in nl:
                    exe = p.info.get('exe')
                    if exe and os.path.exists(exe):
                        return exe
            except Exception:
                continue
    except Exception:
        return None
    return None


def _files_cover_placeholder(size: int, radius: int) -> QPixmap:
    """Default cover for local files that ship no embedded art: dark tile with a
    centered folder glyph (so it reads as 'a local file', not blank/stale art)."""
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_c('sunk')))
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    g = _folder_pixmap(28)
    p.drawPixmap(int((size - g.width()) / 2), int((size - g.height()) / 2), g)
    p.end()
    return pm


class _PlayingBars(QWidget):
    """_PlayingBars"""

    def __init__(self):
        super().__init__()
        self._n = 4
        self._t = 0.0
        self._active = False
        self._speeds = [8.5, 12.5, 6.5, 10.5]
        self._phases = [random.uniform(0, 6.28) for _ in range(self._n)]
        self.setFixedSize(_s(20), _s(16))
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    def set_active(self, on: bool):
        on = bool(on)
        if on == self._active:
            return None
        self._active = on
        self._refresh_timer()

    def _refresh_timer(self):
        should_run = self._active and self.isVisible()
        if should_run:
            if not self._timer.isActive():
                self._timer.start()
            return None
        if not should_run:
            if self._timer.isActive():
                self._timer.stop()
                self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._refresh_timer()

    def _tick(self):
        self._t += self._timer.interval() / 1000.0
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_c('icon_dim')))
        w, h = self.width(), self.height()
        bw = w / (self._n * 2 - 1)
        for i in range(self._n):
            if self._active:
                hf = 0.55 + 0.45 * math.sin(self._t * self._speeds[i] + self._phases[i])
            else:
                hf = 0.22
            bh = max(2.0, h * hf)
            p.drawRoundedRect(QRectF(i * bw * 2, h - bh, bw, bh), bw * 0.35, bw * 0.35)


class _ToggleButton(QPushButton):
    """_ToggleButton"""

    def __init__(self, text: str, kind: str, object_name: str = 'togglebtn'):
        super().__init__(text)
        self.setObjectName(object_name)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty('active', 'false')
        self._kind = kind
        self._on_icon = _kind_icon(kind, _c('emph_text'))
        self._off_icon = _kind_icon(kind, _c('icon'))
        self.setIcon(self._off_icon)
        self.setIconSize(QSize(_s(19), _s(19)))

    def _retint(self):
        """Rebuild the glyph icons for the active theme (live theme switch)."""
        self._on_icon = _kind_icon(self._kind, _c('emph_text'))
        self._off_icon = _kind_icon(self._kind, _c('icon'))
        self.setIcon(self._on_icon if self.property('active') == 'true' else self._off_icon)

    def set_state(self, on: bool):
        on = bool(on)
        if (self.property('active') == 'true') == on:
            return None
        self.setProperty('active', 'true' if on else 'false')
        self.setIcon(self._on_icon if on else self._off_icon)
        self.style().unpolish(self)
        self.style().polish(self)


def _tip(text, w=300):
    """Wrap tooltip text so Qt word-wraps it to ~`w` px instead of one giant
    line (a td width is the one rich-text width Qt reliably honours)."""
    return f"<table><tr><td width='{_s(w)}'>{text.replace(chr(10), '<br>')}</td></tr></table>"


class _InfoLabel(QLabel):
    """Circled-i tooltip target that brightens on hover."""

    def __init__(self, tip: str, size: int = 15):
        super().__init__()
        self._off = _info_icon(size, bright=False)
        self._on = _info_icon(size, bright=True)
        self.setFixedSize(_s(size), _s(size))
        self.setPixmap(self._off)
        self.setToolTip(_tip(tip))

    def enterEvent(self, e):
        self.setPixmap(self._on)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.setPixmap(self._off)
        super().leaveEvent(e)


class _ComboItemDelegate(QStyledItemDelegate):
    """Selected combo item = a rounded, inset accent pill (the native Win11
    selection shape, but in the Segue colour). The Windows style ignores QSS
    ::item rounding/margin, so the selection is painted here instead."""

    def paint(self, painter, option, index):
        if option.state & QStyle.StateFlag.State_Selected:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            r = QRectF(option.rect).adjusted(_s(3), _s(1), -_s(3), -_s(1))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(_ACCENT))
            painter.drawRoundedRect(r, _s(5), _s(5))
            painter.setPen(QColor(_c('emph_text')))
            painter.drawText(QRectF(option.rect).adjusted(_s(10), 0, -_s(8), 0), int(Qt.AlignVCenter | Qt.AlignLeft), str(index.data()))
            painter.restore()
            return None
        super().paint(painter, option, index)


class _InfoPopupLabel(QLabel):
    """Circled-i that shows a rich hover popup (built by `factory`) instead of a
    plain text tooltip - tucks the gesture GIFs into a hover so they don't
    clutter the Controls window."""

    def __init__(self, factory, size: int = 15):
        super().__init__()
        self._factory = factory
        self._off = _info_icon(size, bright=False)
        self._on = _info_icon(size, bright=True)
        self.setFixedSize(_s(size), _s(size))
        self.setPixmap(self._off)
        self.setCursor(Qt.WhatsThisCursor)
        self._popup = None

    def event(self, e):
        if e.type() == QEvent.ToolTip:
            return True
        return super().event(e)

    def _hide_popup(self):
        if self._popup is not None:
            self._popup.hide()
            self._popup.deleteLater()
            self._popup = None

    def enterEvent(self, e):
        self.setPixmap(self._on)
        self._hide_popup()
        from PySide6.QtWidgets import QFrame, QVBoxLayout
        from PySide6.QtCore import QPoint
        pop = QFrame(self)
        pop.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        pop.setAttribute(Qt.WA_TranslucentBackground, True)
        _outer = QVBoxLayout(pop)
        _outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame(pop)
        card.setObjectName('infopop')
        card.setStyleSheet(f'QFrame#infopop{{background:{_c("sunk")};border:1px solid {_c("border")};border-radius:{_s(10)}px;}}')
        _outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(_s(12), _s(12), _s(12), _s(12))
        try:
            lay.addWidget(self._factory())
        except Exception:
            pass
        pop.adjustSize()
        g = self.mapToGlobal(QPoint(self.width() // 2 - pop.width() // 2, self.height() + _s(6)))
        pop.move(g)
        pop.show()
        pop.raise_()
        self._popup = pop
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.setPixmap(self._off)
        self._hide_popup()
        super().leaveEvent(e)


def _setup_quick_look(device: str, mode: str = 'forza', preset: str = '') -> tuple | None:
    """Compact controls preview at the end of every Setup variant. Per-device
    AND per-mode: in Forza mode PlayStation users skip via D-pad; in general
    mode they swipe horizontally because the D-pad belongs to the game."""
    if device == 'playstation':
        if mode == 'general':
            pause_cap = 'Press → pause / play' if preset == 'rocketleague' else 'Tap → pause / play'
            clips = [('Swipe right → next', 'clips/touchpad_swipe_right.gif'), ('Swipe left → previous', 'clips/touchpad_swipe_left.gif'), ('Swipe up / down → volume', 'clips/touchpad_swipe_up.gif'), (pause_cap, 'clips/touchpad_tap.gif')]
        else:
            clips = [('D-pad Left / Right → skip', 'clips/dpad_press_right_left.gif'), ('Swipe up / down → volume', 'clips/touchpad_swipe_up.gif'), ('Tap → pause / play', 'clips/touchpad_tap.gif')]
        return ('Touchpad controls', ['Tap the Controls tab on the left for full details + other devices.'], [], clips, 2)
    if device == 'xbox':
        return ('Your controls (quick look)', ['D-pad Left / Right skips tracks. Everything else (volume, pause) is bindable - see Controls tab.'], [], [('D-pad Left / Right → skip track', 'clips/dpad_press_right_left.gif')])
    if device == 'wheel':
        return ('Your controls (quick look)', ['If your wheel has a D-pad on the hub, it skips tracks. Bind anything else in Controls → Rebind.'], [], [('D-pad Left / Right → skip track', 'clips/dpad_press_right_left.gif')])


def _build_howitworks_tab(mode: str = 'forza') -> tuple:
    """How it works text adapts to mode: Forza talks about Data Out telemetry
    + driving/menus, general mode talks about game-audio presence + speech."""
    if mode == 'general':
        return ('How it works', [('Spotify follows your game', ["Segue watches the game you've picked and adjusts Spotify on its own - you don't press anything:", '', '•  Playing the game (window focused) - full volume.', '•  Alt-tabbed / minimized - drops to your Unfocused Volume.', '•  Someone talks in-game (if Speech recognition is on) - ducks down, then back up.', '', "Controllers add manual skip / volume / pause on top - they're optional."]), ('No telemetry needed', ["General mode works with any game that makes sound - it doesn't read anything from the game itself. Because of that it can't tell an in-game menu from active play the way Forza mode can; it uses which window is focused instead (that's the Unfocused Volume drop when you tab out)."]), ('Spotify or browser', ["Click the source icon in the top bar (next to the link) to switch between Spotify and your browser - handy if you'd rather watch YouTube. Segue then controls that app's volume instead.", '', 'Browser note: it dims the whole browser, not one tab, and ads play at full volume.'])])
    return ('How it works', [('Spotify becomes your in-game radio', ["Segue reads Forza's Data Out feed and sets Spotify's own volume to match what you're doing. You don't press anything - it reacts on its own:", '', '•  Driving - full volume.', '•  Menus / paused - quiet or muted (your Menu Volume).', '•  Someone talks in-game - ducked down (your Ducked Volume), then back up.', '', "Controllers only add manual skip / volume / pause on top of the automatic behaviour - they're optional."]), ('Spotify or browser', ["Click the source icon in the top bar (next to the link) to switch between Spotify and your browser - handy if you'd rather watch YouTube. Segue then controls that app's volume instead.", '', 'Browser note: it dims the whole browser, not one tab, and ads play at full volume.'])])


def _build_limitations_tab(mode: str = 'forza') -> tuple:
    """Limitations differ in general mode - no Forza-specific stuff about
    garage/Forzavista, but new ones about no menu detection."""
    if mode == 'general':
        return ('Limitations', [('No in-game menu detection', ['Most games don\'t expose a clean "in a menu" signal to other apps, so Segue can\'t tell a pause menu from active play. It uses which window is focused: full volume while the game is focused, Unfocused Volume when you alt-tab out. The proper drive/menu auto-switch only exists in Forza mode (telemetry).']), ("You'll steer it by hand sometimes", ["Apart from the focus + speech auto-adjust, Segue can't read your intent. You'll skip, pause and nudge volume yourself via the touchpad / in-app buttons."]), ("Speech ducking isn't perfect", ["Ducking listens to the picked game's audio and guesses when someone is talking. It can miss a line, duck a beat late, or dip briefly during loud effects. Tune Ducked Volume and Fade length to taste, or turn Speech recognition off in Mixer."]), ('Some games block audio capture', ["A few games (anti-cheat protected, exclusive audio) refuse process-loopback capture - ducking won't fire on those. Volume and skip still work fine."]), ('The overlay only shows in some captures', ["The now-playing overlay appears in display / monitor capture (OBS Display Capture, ShadowPlay desktop) - play borderless windowed. Pure game-capture and exclusive fullscreen won't show it, because Segue never draws inside the game."])])
    return ('Limitations', [('The garage counts as "driving"', ["Forza reports the garage, Forzavista and some menus as a live race, so Segue treats them as driving (full volume, D-pad can skip). That's why Lock Skip exists - turn it on when you're parked so songs don't jump."]), ("You'll steer it by hand sometimes", ["Segue only knows driving vs menu vs speech - it can't read your intent. Sometimes you'll want to skip, pause or change volume yourself; that's what the controller and in-app buttons are for."]), ("Speech ducking isn't perfect", ["Ducking listens to the game's audio and guesses when someone is talking. It can miss a line, duck a beat late, or dip briefly during loud effects. Tune Ducked Volume and Fade length to taste, or turn Speech recognition off if it bothers you."]), ('The overlay only shows in some captures', ["The now-playing overlay appears in display / monitor capture (OBS Display Capture, ShadowPlay desktop) - play borderless windowed. Pure game-capture and exclusive fullscreen won't show it, because Segue never draws inside the game."])])


def _build_bansafe_tab(mode: str = 'forza') -> tuple:
    """Ban-safe story differs per mode - Forza uses official Data Out, general
    mode uses Windows audio APIs only."""
    if mode == 'general':
        return ("Why it's ban-safe", [("What Segue touches - and doesn't", ["In Other-game mode, Segue uses Windows audio APIs only: it listens to the picked process's audio for speech detection (same API OBS / Discord use) and sets Spotify's per-app volume. No game files, no game memory, no code injection, no network calls. Nothing for anti-cheat to flag."]), ('The trade-off', ["Because Segue stays completely outside the game, it can't free its own buttons or draw inside the game for you. The touchpad belongs entirely to Segue and the D-pad belongs to the game - swipe to skip / change volume."]), ('Caveat', ["A few competitive games (anti-cheat protected, exclusive audio) block process-loopback capture. Segue still works for volume and skip, but ducking on speech won't fire."])])
    return ("Why it's ban-safe", [("What Segue touches - and doesn't", ['Segue only reads Forza\'s official "Data Out" telemetry and sets Spotify\'s own volume. It never reads or writes game files, never touches game memory, never injects code, and never sends anything over the network. There is nothing for anti-cheat to flag.']), ('The trade-off', ["Because it stays completely outside the game, Segue can't free its own buttons or draw inside the game for you. That's why you unbind the D-pad in Forza yourself (see Setup) and why the overlay needs display capture. A little manual setup is the price of staying safe - and worth it."])])


def _build_volume_tab(mode: str = 'forza') -> tuple:
    """Volume & mixer help, matched to the sliders the active mode actually
    shows. Forza has Menu Volume (telemetry); general mode has Unfocused
    Volume (focus-driven) instead."""
    if mode == 'general':
        return ('Volume & mixer', [('The volume sliders', ["Volume - your master level (the one your volume buttons / touchpad move). Segue scales Spotify's own volume, so it can't go louder than Spotify itself is set.", '', 'Unfocused Volume - level when you alt-tab out of the game (0 = muted).', '', 'Ducked Volume - level while Segue hears speech (only shown when Speech recognition is on).']), ('Speech & fade', ['Speech recognition - turns ducking on or off. Off hides Ducked Volume + saves CPU.', '', 'Fade length (Extras) - how long music takes to ramp back up after a duck. Longer is smoother but slower to recover.'])])
    return ('Volume & mixer', [('The three levels', ["Volume - your master level. The same one your volume buttons move; applies instantly, in-game or not. Segue scales Spotify's own volume, so it can't go louder than Spotify itself is set.", '', 'Menu Volume - level in menus and when paused (0 = muted).', '', 'Ducked Volume - level while Segue hears speech in-game.']), ('Speech & fade', ['Speech recognition - turns ducking on or off. Off greys out Ducked Volume.', '', 'Fade length (Extras) - how long music takes to ramp back up after a duck or a menu. Longer is smoother but slower to recover.'])])


def _build_troubleshooting_tab(mode: str = 'forza') -> tuple:
    """Troubleshooting matched to mode. Forza items are Data-Out / radio /
    D-pad specific; general items are process-detection / touchpad."""
    if mode == 'general':
        return ('Troubleshooting', [('Game shows "not running"', ["Segue watches the exact process you picked. Launch the game first, then re-pick it via the game chevron if the name doesn't match. Auto-detect also switches when a known game (Forza, Rocket League) starts."]), ('Spotify not playing', ["Open Spotify and start a track. Segue follows whatever Spotify is playing - it doesn't start music on its own."]), ('Controller does nothing', ['Plug the controller in before launching, or wait a couple seconds for Segue to pick it up (it retries). DualSense + DualShock 4 work out of the box; other pads via Controls.']), ('Music too quiet everywhere', ["Raise the Volume slider AND check Spotify's own volume - Segue scales Spotify, it can't push past Spotify's own level."]), ('Music drops when I alt-tab', ["That's Unfocused Volume doing its job. Raise it (or set it to 100 %) in Mixer if you don't want the drop."])])
    return ('Troubleshooting', [('Forza not detected', ['Enable Data Out on 127.0.0.1 port 5300 (Setup step 3).', '', 'Make sure nothing else is using that port.']), ('Spotify not playing', ["Open Spotify and start a track. Segue follows whatever Spotify is playing - it doesn't start music on its own."]), ("D-pad does nothing, or flips Forza's radio", ["You haven't unbound D-pad Left / Right in Forza yet, or the in-game radio is still on. See Setup steps 1 and 2."]), ('Buttons dead while driving (Xbox / keyboard)', ['Forza grabs those same inputs to drive. Free them inside Forza first, then rebind in Segue → Controls.']), ('Music too quiet everywhere', ["Raise the Volume slider in Segue AND check Spotify's own volume - Segue scales Spotify, it can't push past Spotify's own level."]), ('Ducking too slow or jumpy', ['Raise Ducked Volume in Mixer, or tune Fade length in Extras.'])])


def _build_setup_tab(device: str, mode: str = 'forza', preset: str = '') -> tuple:
    """Setup steps tailored to the chosen input device + game preset.

    Per-preset shape:
      - Rocket League: plug-and-play.  No Forza/Data Out lingo, two short
        sentences.
      - Other game (mode=general): pick-from-list + touchpad controls.
      - Forza: the full radio / D-pad / Data Out walkthrough.
    """
    _SPOTIFY_TIP = ('Tip', ["Crank Spotify to 80-100 %. Segue scales down from there - it can't go louder than Spotify's own slider."], ['spotify_volume_tip.png'])
    if preset == 'rocketleague':
        rblocks = [('Quick setup', ["<b>1.</b>  In Rocket League → Settings → Audio, set Music Volume to 0 (so it doesn't clash with Spotify).<br><br><b>2.</b>  Plug in your DualSense or DualShock 4.<br><br><b>3.</b>  Launch Rocket League and play.<br><br>Touchpad swipe left / right skips tracks. Touchpad click (press) pauses and plays. Swipe up / down for volume."]), _SPOTIFY_TIP]
        ql = _setup_quick_look(device, mode, preset)
        if ql is not None:
            rblocks.append(ql)
        return ('Setup', rblocks)
    if mode == 'general':
        gblocks = [('1.  Pick your game', ['Click the chevron next to the game icon at the top → "Other game…" → pick from the list.', '', 'Only apps currently making sound show up, so launch the game first.']), ("2.  Mute the game's music", ["In the game's own audio settings, set its music to 0 so it doesn't clash with Spotify (sound effects can stay up)."]), _SPOTIFY_TIP, ('3.  Play Spotify', ['Open Spotify and start a track. Segue controls its volume.']), ('4.  Use the controls', ['Touchpad swipe = skip / volume. Tap = pause. The D-pad belongs to the game by default - skip via touchpad instead.', '', "Speech ducking still works on the picked process - turn it off in Mixer if you don't want it."]), ('Note', ["General mode has no menu / paused detection (most games don't expose that). Music stays at Drive volume until you pause Spotify or change it yourself."])]
        ql = _setup_quick_look(device, mode)
        if ql is not None:
            gblocks.append(ql)
        return ('Setup', gblocks)
    uses_dpad = device in ('playstation', 'dualsense', 'dualshock', 'xbox')
    is_playstation = device in ('playstation', 'dualsense', 'dualshock')
    blocks = []
    n = 1
    if uses_dpad:
        radio_body = ['Forza → pause → Audio → Radio: Off.', '', "Until you do this, pressing D-pad Left / Right will skip Forza's radio stations instead of your Spotify tracks."]
    else:
        radio_body = ['Forza → pause → Audio → Radio: Off.', '', "Otherwise Forza's own radio plays on top of Spotify and they fight each other."]
    blocks.append((f'{n}.  Turn the in-game radio OFF', radio_body, ['setup_radio.png']))
    n += 1
    blocks.append((f"{n}.  Turn the game's music OFF", ['Forza → pause → Audio → set Music to 0 (the menu / title-screen music).', '', "Otherwise it plays over Spotify whenever you're not out driving."], ['setup_music.png']))
    n += 1
    if uses_dpad:
        blocks.append((f'{n}.  Unbind the D-pad in Forza', ['Forza → Settings → Controls → unbind Radio Previous and Radio Next (D-pad Left / Right).', '', 'Now Segue owns the D-pad and can skip Spotify tracks.'], ['setup_controls.png']))
        n += 1
        if is_playstation:
            blocks.append((f'{n}.  Set up the LINK wheel (no skip clashes)', ["ANNA and Forza LINK are picked with the D-pad - the same buttons Segue skips with. So when you open a wheel, Segue locks skipping for 3 seconds: pick your phrase, then skip's back. ANNA opens on D-pad Down, so it's already covered.", '', 'Forza LINK just needs two quick rebinds in Forza → Settings → Controls:', '', 'a)  Set "Map" to Unbound. You won\'t miss it - the map is still one tap away via the Menu button (first option, already highlighted).', ('img', 'setup_link1.png'), 'b)  Bind "Forza LINK" to the View button - the Create / Share button, just left of the touchpad.', ('img', 'setup_link2.png'), '', 'Done. Pressing View now opens LINK and arms the same skip-lock, so picking a phrase never skips a track.']))
            n += 1
    blocks.append((f'{n}.  Turn on Data Out', ['Forza → Settings → HUD & Gameplay → Telemetry, then set:', '      Data Out:                 On', '      Data Out IP Address:  127.0.0.1', '      Data Out IP Port:        5300', 'Same steps in Horizon 4 and 5 - Segue works with all of them.', '(Click an image below to see it full-size.)'], ['setup_hud.png', 'setup_telemetry.png']))
    n += 1
    blocks.append((f'{n}.  Play Spotify', ['Open Spotify and start a track. Segue follows whatever Spotify is playing.']))
    n += 1
    blocks.append(_SPOTIFY_TIP)
    blocks.append((f'{n}.  Launch Forza', ['Tick "Auto-start" in Extras to launch Segue with the game, or just run it yourself.', '', 'The top bar turns green when both Forza and Spotify are detected.']))
    blocks.append(('Playing on Xbox? (experimental)', ['Forza on the console, music on this PC - Segue can follow the game over your home network:', '', '1)  Find this PC\'s local IP: press Win+R, run "cmd", type "ipconfig" - grab the IPv4 address (looks like 192.168.x.x).', "2)  In Forza ON THE XBOX: Settings → HUD & Gameplay → Telemetry → Data Out: On, IP Address = your PC's IP, Port 5300.", '3)  Run Segue here with the Forza preset, music on this PC. If Windows asks about the firewall, allow on Private networks.', '', 'Menus and races now drive your music volume just like on PC. Controller buttons stay PC-only (your pad talks to the console) - keyboard binds and Ctrl+Shift+Alt+S still work. The status bar may say "Forza not detected"; that\'s cosmetic, telemetry still flows.', '', 'Tip: listen to the console through the Xbox app (Remote Play) on this PC and turn on "Include Discord" - speech ducking then works on the game audio too.', '', "No data arriving? Forza's console build drops Data Out sometimes. The fix that works: re-enter the IP and port in Forza (even if they look right), then FULLY quit the game - highlight it on the dashboard, Menu button, Quit. Quick Resume keeps dead connections alive, so the Home button isn't enough. Relaunch and drive."]))
    ql = _setup_quick_look(device, mode)
    if ql is not None:
        blocks.append(ql)
    return ('Setup', blocks)


def _build_controls_tab(device: str, mode: str = 'forza', preset: str = '') -> tuple:
    """Controls help with looping GIF previews per gesture. PlayStation gets
    the touchpad set; in Forza mode D-pad is the primary skip, in general
    mode horizontal swipe handles skip (D-pad belongs to the game). Xbox
    gets D-pad clip; keyboard / wheel stay text-only."""
    blocks = []
    blocks.append(('Pick a device', ['Controls → Select Device. PlayStation (Best) gives full touchpad control out of the box; Xbox + keyboard (Beta) and sim wheel (Beta) are fully bindable. Switching device applies right away - no restart needed.']))
    if device == 'playstation':
        if mode == 'general':
            pause_is_press = preset == 'rocketleague'
            pause_caption = 'Touchpad press → pause / play' if pause_is_press else 'Tap → pause / play'
            ps_clips = [('Swipe right → next track', 'clips/touchpad_swipe_right.gif', 150), ('Swipe left → previous track', 'clips/touchpad_swipe_left.gif', 150), ('Swipe up / down → volume', 'clips/touchpad_swipe_up.gif', 150), (pause_caption, 'clips/touchpad_tap.gif', 150)]
            blocks.append(('PlayStation defaults', [], [], ps_clips, 2))
            blocks.append(('Also', ['•  Skip can also be the left-stick click (L3) - switch it in Controls → Skip with.', '•  Mute button  →  Lock Skip.']))
        else:
            ps_clips = [('D-pad Left / Right → skip track', 'clips/dpad_press_right_left.gif', 150), ('Swipe up / down → volume', 'clips/touchpad_swipe_up.gif', 150), ('Tap → pause / play', 'clips/touchpad_tap.gif', 150)]
            blocks.append(('PlayStation defaults', [], [], ps_clips, 2))
            blocks.append(('Also', ['•  Mute button  →  Lock Skip', "•  Skipping only fires while you're actually in a race, so it can't trigger by accident in menus."]))
    elif device == 'xbox':
        blocks.append(('Xbox defaults', ['•  D-pad Left / Right  →  skip track', '', 'Everything else (volume, pause) is bindable in Controls → Rebind.'], [], [('D-pad Left / Right → skip track', 'clips/dpad_press_right_left.gif')]))
    elif device == 'keyboard':
        blocks.append(('Keyboard defaults', ['•  [ ]    →  skip prev / next', '•  - =    →  volume down / up', '•  \\     →  pause / play', '•  ;      →  Lock Skip', '', 'Rebind in Controls → Rebind if any of these collide with another app.']))
    else:
        blocks.append(('Sim wheel', ['All buttons are bindable in Controls → Rebind. No fixed scheme since wheels vary wildly. If your wheel has a D-pad on the hub (Logitech G29, Thrustmaster T300, etc.), the example below is what skipping looks like.'], [], [('D-pad Left / Right → skip track', 'clips/dpad_press_right_left.gif')]))
    blocks.append(('Lock Skip & Turn Off', ['Lock Skip - freezes track-skipping. Toggle it on the controller or in the app. See Limitations for why the garage needs it.', '', 'Turn Off - hands Spotify back to you and stops Segue touching the volume, without closing the app. Turn On to resume.']))
    return ('Controls', blocks)
_HELP_TABS = [('Setup', [('1.  Turn the in-game radio OFF', ['Forza → pause → Audio → Radio: Off.', '', "Until you do this, pressing D-pad Left / Right will skip Forza's radio stations instead of your Spotify tracks."], ['setup_radio.png']), ("2.  Turn the game's music OFF", ['Forza → pause → Audio → set Music to 0 (the menu / title-screen music).', '', "Otherwise it plays over Spotify whenever you're not out driving."], ['setup_music.png']), ('3.  Unbind the D-pad in Forza', ['Forza → Settings → Controls → unbind Radio Previous and Radio Next (D-pad Left / Right).', '', 'Now Segue owns the D-pad and can skip Spotify tracks.'], ['setup_controls.png']), ('4.  Set up the LINK wheel (no skip clashes)', ["ANNA and Forza LINK are picked with the D-pad - the same buttons Segue skips with. So when you open a wheel, Segue locks skipping for 3 seconds: pick your phrase, then skip's back. ANNA opens on D-pad Down, so it's already covered.", '', 'Forza LINK just needs two quick rebinds in Forza → Settings → Controls:', '', 'a)  Set "Map" to Unbound. You won\'t miss it - the map is still one tap away via the Menu button (first option, already highlighted).', ('img', 'setup_link1.png'), 'b)  Bind "Forza LINK" to the View button - the Create / Share button, just left of the touchpad.', ('img', 'setup_link2.png'), '', 'Done. Pressing View now opens LINK and arms the same skip-lock, so picking a phrase never skips a track.']), ('5.  Turn on Data Out', ['Forza → Settings → HUD & Gameplay → Telemetry, then set:', '      Data Out:                 On', '      Data Out IP Address:  127.0.0.1', '      Data Out IP Port:        5300', 'Same steps in Horizon 4 and 5 - Segue works with all of them.', '(Click an image below to see it full-size.)'], ['setup_hud.png', 'setup_telemetry.png']), ('6.  Play Spotify', ['Open Spotify and start a track. Segue follows whatever Spotify is playing.']), ('Tip', ["Crank Spotify to 80-100 %. Segue scales down from there - it can't go louder than Spotify's own slider."], ['spotify_volume_tip.png']), ('7.  Launch Forza', ['Tick "Auto-start" in Extras to launch Segue with the game, or just run it yourself.', '', 'The top bar turns green when both Forza and Spotify are detected.']), ('Playing on Xbox? (experimental)', ['Forza on the console, music on this PC - Segue can follow the game over your home network:', '', '1)  Find this PC\'s local IP: press Win+R, run "cmd", type "ipconfig" - grab the IPv4 address (looks like 192.168.x.x).', "2)  In Forza ON THE XBOX: Settings → HUD & Gameplay → Telemetry → Data Out: On, IP Address = your PC's IP, Port 5300.", '3)  Run Segue here with the Forza preset, music on this PC. If Windows asks about the firewall, allow on Private networks.', '', 'Menus and races now drive your music volume just like on PC. Controller buttons stay PC-only (your pad talks to the console) - keyboard binds and Ctrl+Shift+Alt+S still work. The status bar may say "Forza not detected"; that\'s cosmetic, telemetry still flows.', '', 'Tip: listen to the console through the Xbox app (Remote Play) on this PC and turn on "Include Discord" - speech ducking then works on the game audio too.', '', "No data arriving? Forza's console build drops Data Out sometimes. The fix that works: re-enter the IP and port in Forza (even if they look right), then FULLY quit the game - highlight it on the dashboard, Menu button, Quit. Quick Resume keeps dead connections alive, so the Home button isn't enough. Relaunch and drive."]), ('Touchpad controls', ['Tap the Controls tab on the left for full details + other devices.'], [], [('D-pad Left / Right → skip', 'clips/dpad_press_right_left.gif'), ('Swipe up / down → volume', 'clips/touchpad_swipe_up.gif'), ('Tap → pause / play', 'clips/touchpad_tap.gif')], 2)]), ('How it works', [('Spotify becomes your in-game radio', ["Segue reads Forza's Data Out feed and sets Spotify's own volume to match what you're doing. You don't press anything - it reacts on its own:", '', '•  Driving - full volume.', '•  Menus / paused - quiet or muted (your Menu Volume).', '•  Someone talks in-game - ducked down (your Ducked Volume), then back up.', '', "Controllers only add manual skip / volume / pause on top of the automatic behaviour - they're optional."]), ('Spotify or browser', ["Click the source icon in the top bar (next to the link) to switch between Spotify and your browser - handy if you'd rather watch YouTube. Segue then controls that app's volume instead.", '', 'Browser note: it dims the whole browser, not one tab, and ads play at full volume.'])]), ('Volume & mixer', [('The three levels', ["Volume - your master level. The same one your volume buttons move; applies instantly, in-game or not. Segue scales Spotify's own volume, so it can't go louder than Spotify itself is set.", '', 'Menu Volume - level in menus and when paused (0 = muted, the default).', '', 'Ducked Volume - level while Segue hears speech in-game.']), ('Speech & fade', ['Speech recognition - turns ducking on or off. Off greys out Ducked Volume.', '', 'Fade length (Extras) - how long music takes to ramp back up after a duck or a menu. Longer is smoother but slower to recover.'])]), ('Controls', []), ('Limitations', []), ("Why it's ban-safe", []), ('Disable & uninstall', [('Pause it', ["Hit Turn Off to hand Spotify's volume back to you. Segue stays open but stops touching the volume; Turn On to resume."]), ('Stop it auto-starting', ['Untick "Auto-start" in Extras so it no longer launches with your games.']), ('Quit', ["☰ → Quit Segue, or close the window. Spotify's volume is restored on exit."]), ('Uninstall', ['Use ☰ → Uninstall Segue, or Windows Settings → Apps → Segue → Uninstall. The uninstaller stops Segue cleanly, removes the app, the autostart shortcut, and your settings in %APPDATA%\\Segue. Nothing left behind.'])]), ('Troubleshooting', [('Forza not detected', ['Enable Data Out on 127.0.0.1 port 5300 (Setup step 3).', '', 'Make sure nothing else is using that port.']), ('Spotify not playing', ["Open Spotify and start a track. Segue follows whatever Spotify is playing - it doesn't start music on its own."]), ("D-pad does nothing, or flips Forza's radio", ["You haven't unbound D-pad Left / Right in Forza yet, or the in-game radio is still on. See Setup steps 1 and 2."]), ('Buttons dead while driving (Xbox / keyboard)', ['Forza grabs those same inputs to drive. Free them inside Forza first, then rebind in Segue → Controls.']), ('Music too quiet everywhere', ["Raise the Volume slider in Segue AND check Spotify's own volume - Segue scales Spotify, it can't push past Spotify's own level."]), ('Ducking too slow or jumpy', ['Raise Ducked Volume in Mixer, or tune Fade length in Extras.'])])]



class _HelpWindow(QWidget):
    """Help window: a vertical section list on the left, content on the right.

    `input_device` selects which Setup steps to show (D-pad-using devices get
    the Forza-unbind step, keyboard / wheel skip it)."""

    _DEV_LABEL = {'playstation': 'PlayStation', 'dualsense': 'DualSense', 'dualshock': 'DualShock 4', 'xbox': 'Xbox', 'keyboard': 'Keyboard', 'wheel': 'Sim wheel'}

    def __init__(self, input_device: str = 'playstation', mode: str = 'forza', on_open_controls=None, preset: str = ''):
        super().__init__()
        self._on_open_controls = on_open_controls
        self._preset = preset

        def _idx(name):
            return next((i for i, (n, _) in enumerate(_HELP_TABS) if n == name), None)

        self._tabs = list(_HELP_TABS)
        self._tabs[0] = _build_setup_tab(input_device, mode, preset)
        hi = _idx('How it works')
        if hi is not None:
            self._tabs[hi] = _build_howitworks_tab(mode)
        vi = _idx('Volume & mixer')
        if vi is not None:
            self._tabs[vi] = _build_volume_tab(mode)
        ci = _idx('Controls')
        if ci is not None:
            self._tabs[ci] = _build_controls_tab(input_device, mode, preset)
        li = _idx('Limitations')
        if li is not None:
            self._tabs[li] = _build_limitations_tab(mode)
        bi = _idx("Why it's ban-safe")
        if bi is not None:
            self._tabs[bi] = _build_bansafe_tab(mode)
        ti = _idx('Troubleshooting')
        if ti is not None:
            self._tabs[ti] = _build_troubleshooting_tab(mode)
        self.setWindowTitle('Segue - Help')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self.setFixedSize(_s(560), _s(560))
        outer = QHBoxLayout(self)
        outer.setContentsMargins(_s(12), _s(12), _s(12), _s(12))
        outer.setSpacing(_s(12))
        nav = QListWidget()
        nav.setObjectName('helpnav')
        nav.setFont(_ui_font(14))
        nav.setFixedWidth(_s(168))
        nav.setCursor(Qt.PointingHandCursor)
        nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav.setTextElideMode(Qt.ElideNone)
        self._setup_device = input_device
        self._curated_preset = preset if preset in ('forza', 'rocketleague') else 'forza'
        self._setup_view_mode = self._curated_preset if mode != 'general' or preset == 'rocketleague' else 'general'
        stack = QStackedWidget()
        for tabname, blocks in self._tabs:
            nav.addItem(tabname)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            _smooth_scroll(scroll)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body = QWidget()
            v = QVBoxLayout(body)
            v.setContentsMargins(_s(6), _s(6), _s(12), _s(12))
            v.setSpacing(_s(8))
            title = QLabel(tabname)
            title.setFont(_ui_font(20, QFont.Bold))
            title.setStyleSheet(f"color: {_c('text')};")
            v.addWidget(title)
            v.addSpacing(_s(4))
            if tabname == 'Controls':
                row = QHBoxLayout()
                row.setSpacing(_s(10))
                cur_label = self._DEV_LABEL.get(self._setup_device, self._setup_device.title() if self._setup_device else '?')
                dev_lbl = QLabel(f"Current device: <b style='color:{_ACCENT};'>{cur_label}</b>")
                dev_lbl.setFont(_ui_font(14))
                dev_lbl.setStyleSheet(f"color: {_c('text')};")
                dev_lbl.setTextFormat(Qt.RichText)
                row.addWidget(dev_lbl)
                row.addStretch(1)
                open_btn = QPushButton('Open Controls →')
                open_btn.setObjectName('togglebtn')
                open_btn.setCursor(Qt.PointingHandCursor)
                open_btn.setEnabled(self._on_open_controls is not None)
                open_btn.clicked.connect(self._invoke_open_controls)
                row.addWidget(open_btn)
                v.addLayout(row)
                v.addSpacing(_s(8))
            from fh6_spotify import game_presets as _gp
            if tabname == 'Setup':
                self._setup_toggle_row = QHBoxLayout()
                self._setup_toggle_row.setSpacing(_s(6))
                _pick_lbl = QLabel('Show setup for:')
                _pick_lbl.setObjectName('hint')
                _pick_lbl.setFont(_ui_font(14))
                self._setup_toggle_row.addWidget(_pick_lbl)
                curated_label = _gp.label_for(self._curated_preset)
                self._setup_forza_btn = QPushButton(curated_label)
                self._setup_other_btn = QPushButton('Other game')
                for btn in (self._setup_forza_btn, self._setup_other_btn):
                    btn.setObjectName('togglebtn')
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setCheckable(True)
                self._setup_forza_btn.clicked.connect(lambda: self._switch_setup_view(self._curated_preset))
                self._setup_other_btn.clicked.connect(lambda: self._switch_setup_view('general'))
                self._setup_toggle_row.addWidget(self._setup_forza_btn)
                self._setup_toggle_row.addWidget(self._setup_other_btn)
                self._setup_toggle_row.addStretch(1)
                v.addLayout(self._setup_toggle_row)
                v.addSpacing(_s(12))
                self._setup_body = QWidget()
                self._setup_body_v = QVBoxLayout(self._setup_body)
                self._setup_body_v.setContentsMargins(0, 0, 0, 0)
                self._setup_body_v.setSpacing(_s(8))
                v.addWidget(self._setup_body)
                self._update_setup_toggle_state()
                self._render_setup_body(blocks)
                v.addStretch(1)
                scroll.setWidget(body)
                stack.addWidget(scroll)
                continue
            for i, blk in enumerate(blocks):
                sub, lines = blk[0], blk[1]
                imgs = blk[2] if len(blk) > 2 else []
                clips = blk[3] if len(blk) > 3 else []
                clip_cols = blk[4] if len(blk) > 4 else 0
                if i:
                    v.addSpacing(_s(14))
                if sub:
                    h = QLabel(sub)
                    h.setFont(_ui_font(16, QFont.Bold))
                    h.setWordWrap(True)
                    h.setStyleSheet(f"color: {_c('text')};")
                    v.addWidget(h)
                if lines:
                    para = QLabel('\n'.join(lines))
                    para.setFont(_ui_font(14))
                    para.setWordWrap(True)
                    para.setStyleSheet(f"color: {_c('text_dim')};")
                    para.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
                    v.addWidget(para)
                for img_name in imgs:
                    p_img = os.path.join(_ASSETS, img_name)
                    if not os.path.exists(p_img):
                        continue
                    pm = QPixmap(p_img)
                    if pm.isNull():
                        continue
                    img_lbl = QLabel()
                    pm_scaled = pm.scaledToWidth(_s(260), Qt.SmoothTransformation)
                    if pm_scaled.height() > _s(220):
                        pm_scaled = pm.scaledToHeight(_s(220), Qt.SmoothTransformation)
                    img_lbl.setPixmap(pm_scaled)
                    img_lbl.setStyleSheet(f"border: 1px solid {_c('border')}; border-radius: {_s(4)}px;")
                    img_lbl.setFixedSize(pm_scaled.size())
                    img_lbl.setAlignment(Qt.AlignLeft)
                    img_lbl.setCursor(Qt.PointingHandCursor)
                    img_lbl.setToolTip('Click to view full size')
                    img_lbl.mousePressEvent = lambda _e, path=p_img: self._open_image_external(path)
                    v.addSpacing(_s(4))
                    v.addWidget(img_lbl)
                from PySide6.QtGui import QImageReader

                def _make_clip_cell(entry, default_w):
                    """Build a (caption + GIF) cell widget. Returns None if
                    the clip file is missing."""
                    clip_name, caption = entry[0], entry[1]
                    custom_w = entry[2] if len(entry) > 2 else None
                    p_clip = os.path.join(_ASSETS, clip_name)
                    if not os.path.exists(p_clip):
                        return None
                    src = QImageReader(p_clip).size()
                    target_w = _s(custom_w if custom_w else default_w)
                    if src.isValid() and src.width() > 0:
                        target_h = int(target_w * src.height() / src.width())
                    else:
                        target_h = _s(int(default_w * 0.6))
                    cell = QWidget()
                    cv = QVBoxLayout(cell)
                    cv.setContentsMargins(0, 0, 0, 0)
                    cv.setSpacing(_s(4))
                    cap = QLabel(caption)
                    cap.setFont(_ui_font(11 if target_w < _s(180) else 13, QFont.Bold))
                    cap.setStyleSheet(f"color: {_c('text')};")
                    cap.setWordWrap(True)
                    cv.addWidget(cap)
                    clip_lbl = _RoundedMovieLabel(_s(6))
                    movie = QMovie(p_clip)
                    movie.setScaledSize(QSize(target_w, target_h))
                    clip_lbl.setMovie(movie)
                    movie.start()
                    clip_lbl.setFixedSize(QSize(target_w, target_h))
                    clip_lbl.setAlignment(Qt.AlignLeft)
                    cv.addWidget(clip_lbl)
                    cell.setFixedWidth(target_w)
                    return cell
                if clip_cols > 0 and clips:
                    v.addSpacing(_s(8))
                    grid = QGridLayout()
                    grid.setHorizontalSpacing(_s(10))
                    grid.setVerticalSpacing(_s(10))
                    for idx, entry in enumerate(clips):
                        cell = _make_clip_cell(entry, default_w=110)
                        if cell is None:
                            continue
                        grid.addWidget(cell, idx // clip_cols, idx % clip_cols, Qt.AlignLeft | Qt.AlignTop)
                    wrap = QWidget()
                    wrap.setLayout(grid)
                    v.addWidget(wrap, 0, Qt.AlignLeft)
                else:
                    for entry in clips:
                        v.addSpacing(_s(8))
                        cell = _make_clip_cell(entry, default_w=220)
                        if cell is None:
                            continue
                        v.addWidget(cell, 0, Qt.AlignLeft)
            v.addStretch(1)
            scroll.setWidget(body)
            stack.addWidget(scroll)
        nav.currentRowChanged.connect(stack.setCurrentIndex)
        nav.setCurrentRow(0)
        nav_col = QVBoxLayout()
        nav_col.setContentsMargins(0, 0, 0, 0)
        nav_col.setSpacing(_s(6))
        _nav_hdr = QLabel('HELP TOPICS')
        _nav_hdr.setObjectName('hint')
        _nav_hdr.setFont(_ui_font(11, QFont.Bold))
        _nav_hdr.setStyleSheet(f"color: {_c('text_hint')}; letter-spacing: 1px;")
        nav_col.addWidget(_nav_hdr)
        nav_col.addWidget(nav, 1)
        outer.addLayout(nav_col)
        outer.addWidget(stack, 1)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('bg')))
        return None

    def _invoke_open_controls(self) -> None:
        """Close Help + open the Controls / Rebind dialog via the callback the
        SettingsWindow handed us. Help reopens fine after the dialog closes."""
        cb = self._on_open_controls
        if cb is None:
            return None
        try:
            self.close()
        except Exception:
            pass
        try:
            cb()
        except Exception:
            return None
        return None

    def _update_setup_toggle_state(self) -> None:
        """Reflect the current preview view on the two toggle buttons.
        'curated' = the left button (Forza / Rocket League), 'general' =
        the right (Other game)."""
        curated = self._setup_view_mode != 'general'
        self._setup_forza_btn.setChecked(curated)
        self._setup_other_btn.setChecked(not curated)
        for btn, on in ((self._setup_forza_btn, curated), (self._setup_other_btn, not curated)):
            btn.setProperty('active', on)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _switch_setup_view(self, view: str) -> None:
        """Re-render the Setup body for the picked preview view without
        touching cfg.mode.  `view` is either the curated preset key
        ('forza' / 'rocketleague') or 'general' (Other game)."""
        if view == self._setup_view_mode:
            self._update_setup_toggle_state()
            return None
        self._setup_view_mode = view
        if view == 'general':
            bmode, bpreset = 'general', ''
        elif view == 'rocketleague':
            bmode, bpreset = 'general', 'rocketleague'
        else:
            bmode, bpreset = 'forza', 'forza'
        _, new_blocks = _build_setup_tab(self._setup_device, bmode, bpreset)
        while self._setup_body_v.count():
            item = self._setup_body_v.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._update_setup_toggle_state()
        self._render_setup_body(new_blocks)

    def _render_setup_body(self, blocks) -> None:
        """Render Setup blocks into the swappable container (calls into the
        same block-renderer the other tabs use)."""
        self._render_blocks_into(self._setup_body_v, blocks)

    def _tip_callout(self, lines) -> QWidget:
        """Styled 'Tip' card: tinted rounded box with an accent left edge,
        a lightbulb glyph + 'Tip' label, and the body text. Reads as advice,
        not a numbered step."""
        card = QFrame()
        card.setObjectName('tipcard')
        cl = QHBoxLayout(card)
        cl.setContentsMargins(_s(12), _s(10), _s(12), _s(10))
        cl.setSpacing(_s(10))
        bulb = QLabel()
        bulb.setPixmap(_bulb_pixmap(20))
        bulb.setAlignment(Qt.AlignTop)
        bulb.setFixedWidth(_s(24))
        cl.addWidget(bulb, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(_s(3))
        head = QLabel('Tip')
        head.setFont(_ui_font(13, QFont.Bold))
        head.setStyleSheet(f"color: {_ACCENT};")
        col.addWidget(head)
        body = QLabel('\n'.join(lines))
        body.setFont(_ui_font(14))
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {_c('text_dim')};")
        col.addWidget(body)
        cl.addLayout(col, 1)
        return card

    def _add_setup_image(self, v, img_name) -> None:
        """Append one inline screenshot (capped, bordered, click-to-zoom) to
        layout `v`. No-op if the asset is missing/unreadable."""
        p_img = os.path.join(_ASSETS, img_name)
        if not os.path.exists(p_img):
            return None
        pm = QPixmap(p_img)
        if pm.isNull():
            return None
        img_lbl = QLabel()
        pm_scaled = pm.scaledToWidth(_s(260), Qt.SmoothTransformation)
        if pm_scaled.height() > _s(220):
            pm_scaled = pm.scaledToHeight(_s(220), Qt.SmoothTransformation)
        img_lbl.setPixmap(pm_scaled)
        img_lbl.setStyleSheet(f"border: 1px solid {_c('border')}; border-radius: {_s(4)}px;")
        img_lbl.setFixedSize(pm_scaled.size())
        img_lbl.setAlignment(Qt.AlignLeft)
        img_lbl.setCursor(Qt.PointingHandCursor)
        img_lbl.setToolTip('Click to view full size')
        img_lbl.mousePressEvent = lambda _e, path=p_img: self._open_image_external(path)
        v.addSpacing(_s(4))
        v.addWidget(img_lbl)

    def _render_blocks_into(self, v, blocks) -> None:
        """Generic block renderer - paragraphs + images + GIF clips. Used by
        the swappable Setup body. Mirrors what the static tab loop does
        inline; the inline loop will be refactored to call this later.

        A `lines` entry may be a plain string (text) OR an ("img", name)
        tuple, which drops that screenshot inline right where it appears -
        so an image sits beside the step it illustrates instead of every
        image piling up at the bottom of the block. Trailing `imgs` (the
        3rd tuple element) still render after the text for blocks that just
        want one shot at the end."""

        def _flush(run):
            if run:
                para = QLabel('\n'.join(run))
                para.setFont(_ui_font(14))
                para.setWordWrap(True)
                para.setStyleSheet(f"color: {_c('text_dim')};")
                para.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
                v.addWidget(para)
            return []
        for i, blk in enumerate(blocks):
            sub, lines = blk[0], blk[1]
            imgs = blk[2] if len(blk) > 2 else []
            clips = blk[3] if len(blk) > 3 else []
            clip_cols = blk[4] if len(blk) > 4 else 0
            if i:
                v.addSpacing(_s(14))
            if sub == 'Tip':
                v.addWidget(self._tip_callout(lines))
            else:
                if sub:
                    h = QLabel(sub)
                    h.setFont(_ui_font(16, QFont.Bold))
                    h.setWordWrap(True)
                    h.setStyleSheet(f"color: {_c('text')};")
                    v.addWidget(h)
                run = []
                for ln in lines:
                    if isinstance(ln, tuple) and ln and ln[0] == 'img':
                        run = _flush(run)
                        self._add_setup_image(v, ln[1])
                    else:
                        run.append(ln)
                run = _flush(run)
            for img_name in imgs:
                self._add_setup_image(v, img_name)
            from PySide6.QtGui import QImageReader

            def _make_clip_cell(entry, default_w):
                clip_name, caption = entry[0], entry[1]
                custom_w = entry[2] if len(entry) > 2 else None
                p_clip = os.path.join(_ASSETS, clip_name)
                if not os.path.exists(p_clip):
                    return None
                src = QImageReader(p_clip).size()
                target_w = _s(custom_w if custom_w else default_w)
                if src.isValid() and src.width() > 0:
                    target_h = int(target_w * src.height() / src.width())
                else:
                    target_h = _s(int(default_w * 0.6))
                cell = QWidget()
                cv = QVBoxLayout(cell)
                cv.setContentsMargins(0, 0, 0, 0)
                cv.setSpacing(_s(4))
                cap = QLabel(caption)
                cap.setFont(_ui_font(11 if target_w < _s(180) else 13, QFont.Bold))
                cap.setStyleSheet(f"color: {_c('text')};")
                cap.setWordWrap(True)
                cv.addWidget(cap)
                clip_lbl = _RoundedMovieLabel(_s(6))
                movie = QMovie(p_clip)
                movie.setScaledSize(QSize(target_w, target_h))
                clip_lbl.setMovie(movie)
                movie.start()
                clip_lbl.setFixedSize(QSize(target_w, target_h))
                clip_lbl.setAlignment(Qt.AlignLeft)
                cv.addWidget(clip_lbl)
                cell.setFixedWidth(target_w)
                return cell
            if clip_cols > 0 and clips:
                v.addSpacing(_s(8))
                grid = QGridLayout()
                grid.setHorizontalSpacing(_s(10))
                grid.setVerticalSpacing(_s(10))
                for idx, entry in enumerate(clips):
                    cell = _make_clip_cell(entry, default_w=110)
                    if cell is None:
                        continue
                    grid.addWidget(cell, idx // clip_cols, idx % clip_cols, Qt.AlignLeft | Qt.AlignTop)
                wrap = QWidget()
                wrap.setLayout(grid)
                v.addWidget(wrap, 0, Qt.AlignLeft)
            else:
                for entry in clips:
                    v.addSpacing(_s(8))
                    cell = _make_clip_cell(entry, default_w=220)
                    if cell is None:
                        continue
                    v.addWidget(cell, 0, Qt.AlignLeft)

    def _open_image_external(self, path: str) -> None:
        """Open the screenshot in the OS default viewer (Photos on Windows).
        Silent if anything fails - this is a nice-to-have."""
        try:
            if sys.platform == 'win32':
                os.startfile(path)
                return None
            import subprocess
            opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([opener, path])
            return None
        except Exception:
            return None


def _combo_grow(parts, held):
    """Extend the captured combo to the PEAK set held: append any newly-pressed
    parts, never drop on release. `held` is a '+'-joined read; `parts` is the
    ordered list captured so far. Pure (unit-tested)."""
    out = list(parts)
    for p in held.split('+'):
        if p and p not in out:
            out.append(p)
    return out


class _KeycapButton(QPushButton):
    """A clickable key-cap: shows the current bound key and rebinds when
    clicked. Replaces the old 'key text + Rebind button' pair for keyboard,
    matching the key-cap look used in the intro slides."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName('keycapbtn')
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(_ui_font(15, QFont.Bold))
        self.setMinimumWidth(_s(46))
        self.setStyleSheet(f"QPushButton#keycapbtn {{ color:{_c('text')}; background:{_c('deep')}; border:2px solid {_c('border_hi')}; border-radius:{_s(8)}px; padding:{_s(6)}px {_s(16)}px; }} QPushButton#keycapbtn:hover {{ border-color:{_ACCENT}; background:{_c('accent_tint')}; }}")


class _CaptureDialog(QDialog):
    """Modal 'press a button' capture: polls the live controller, shows what was
    pressed, and asks to Confirm or Cancel before applying."""

    def __init__(self, reader, parent=None, kind='button', owner_of=None, device=None):
        super().__init__(parent)
        self._reader = reader
        self._kind = kind
        self.code = None
        self._last_code = None
        self._parts = []
        self._building = False
        self._owner_of = owner_of
        self._back_codes = {'playstation': ('circle',), 'dualsense': ('circle',), 'dualshock': ('circle',), 'xbox': ('btn:1',)}.get(device, ())
        self._confirm_codes = {'playstation': ('cross',), 'dualsense': ('cross',), 'dualshock': ('cross',), 'xbox': ('btn:0',)}.get(device, ())
        self._device = device
        self._prompt_text = 'Press a key, or a combo like Ctrl+Shift+P…' if kind == 'key' else 'Press a button, or hold two together for a combo…'
        self.setWindowTitle('Rebind')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(15))
        self.setMinimumWidth(_s(470))
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(20), _s(18), _s(20), _s(18))
        v.setSpacing(_s(12))
        self._prompt = QLabel(self._prompt_text)
        self._prompt.setObjectName('hint')
        self._prompt.setFont(_ui_font(14))
        self._prompt.setAlignment(Qt.AlignCenter)
        self._prompt.setWordWrap(True)
        v.addWidget(self._prompt)
        self._detected = QLabel('')
        self._detected.setObjectName('nptitle')
        self._detected.setFont(_ui_font(22, QFont.Bold))
        self._detected.setAlignment(Qt.AlignCenter)
        v.addWidget(self._detected)
        self._warn = QLabel('')
        self._warn.setFont(_ui_font(13))
        self._warn.setAlignment(Qt.AlignCenter)
        self._warn.setWordWrap(True)
        self._warn.setStyleSheet(f"color:{_c('danger')};")
        self._warn.hide()
        v.addWidget(self._warn)
        btns = QHBoxLayout()
        cancel = QPushButton('Cancel')
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        self._again = QPushButton('Press again')
        self._again.setCursor(Qt.PointingHandCursor)
        self._again.setMinimumWidth(self._again.fontMetrics().horizontalAdvance('Press again') + _s(28))
        self._again.clicked.connect(self._restart)
        self._again.hide()
        if device in ('playstation', 'dualsense', 'dualshock'):
            _pm = _ps_face_pixmap('circle', _s(18))
            if _pm is not None:
                self._again.setIcon(QIcon(_pm))
                self._again.setIconSize(_pm.size())
                self._again.setMinimumWidth(self._again.minimumWidth() + _s(26))
        elif device == 'xbox':
            self._again.setText('Press again  (B)')
            self._again.setMinimumWidth(self._again.fontMetrics().horizontalAdvance('Press again  (B)') + _s(28))
        self._confirm = QPushButton('Confirm')
        self._confirm.setObjectName('savebtn')
        self._confirm.setCursor(Qt.PointingHandCursor)
        self._confirm.setEnabled(False)
        self._confirm.clicked.connect(self.accept)
        if device in ('playstation', 'dualsense', 'dualshock'):
            _cpm = _ps_face_pixmap('cross', _s(18), _c('emph_text'))
            if _cpm is not None:
                self._confirm.setIcon(QIcon(_cpm))
                self._confirm.setIconSize(_cpm.size())
        unbind = QPushButton('Unbind')
        unbind.setCursor(Qt.PointingHandCursor)
        unbind.setToolTip('Clear this binding (leave it unbound)')
        unbind.clicked.connect(self._unbind)
        btns.addWidget(cancel)
        btns.addWidget(unbind)
        btns.addStretch(1)
        btns.addWidget(self._again)
        btns.addWidget(self._confirm)
        v.addLayout(btns)
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)
        self._baseline = set()
        if kind == 'key':
            self.setFocusPolicy(Qt.StrongFocus)
            return None
        try:
            self._baseline = {p for p in (self._reader.read_pressed() or '').split('+') if p}
        except Exception:
            self._baseline = set()
        self._timer.start()
        return None

    def showEvent(self, e):
        super().showEvent(e)
        if self._kind == 'key':
            self.grabKeyboard()
            return None
        return None

    def _unbind(self):
        try:
            self._timer.stop()
        except Exception:
            pass
        self.code = ''
        self.accept()
        return None

    def keyPressEvent(self, e):
        if self._kind != 'key':
            return super().keyPressEvent(e)
        if e.key() == Qt.Key_Escape:
            self.reject()
            return None
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and (not e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier)) and self.code:
            self.accept()
            return None
        if e.key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr):
            return None
        vk = e.nativeVirtualKey()
        if not vk:
            return None
        mods = e.modifiers()
        parts = []
        if mods & Qt.ControlModifier:
            parts.append(17)
        if mods & Qt.ShiftModifier:
            parts.append(16)
        if mods & Qt.AltModifier:
            parts.append(18)
        parts.append(int(vk))
        self.code = 'key:' + '+'.join(str(p) for p in parts)
        self._prompt.setText('Detected - Confirm to bind, or press again.')
        self._detected.setText(_pretty_code(self.code, self._device))
        self._confirm.setEnabled(True)
        self._again.show()
        self._check_conflict()
        return None

    def _arm_confirm(self, on):
        """Orange outline on Confirm once a bind is detected (it's the obvious
        next click). Empty stylesheet reverts to the app's #savebtn style."""
        if on:
            self._confirm.setStyleSheet(f"QPushButton {{ background:{_c('btn_fill')}; color:{_c('btn_text')}; border:2px solid {_ACCENT}; border-radius:{_s(8)}px; padding:{_s(7)}px {_s(10)}px; font-weight:700; }}QPushButton:hover {{ background:{_c('btn_fill_hi')}; }}")
            return None
        self._confirm.setStyleSheet('')
        return None

    def _check_conflict(self):
        """Show/hide the red 'already bound' warning for the detected code."""
        self._arm_confirm(True)
        owner = self._owner_of(self.code) if self._owner_of and self.code else None
        if owner:
            self._warn.setText(f"Already bound to “{owner}”. Override to move it.")
            self._warn.show()
            self._confirm.setText('Override')
            return None
        self._warn.hide()
        self._confirm.setText('Confirm')
        return None

    def _poll(self):
        """Combo capture by simultaneous hold: hold one or more buttons together,
        the candidate grows to the PEAK set held; release them all to finalize;
        then Confirm. Confirm/back (Cross/Circle, A/B) act as controls ONLY while
        nothing is held - so they're free to be part of a combo when held with
        others, and a lone press after release confirms/re-arms. Pressing a fresh
        button after release starts a NEW combo (sequential taps do NOT merge)."""
        try:
            held = self._reader.read_pressed()
        except Exception:
            held = None
        if held is not None and self._baseline:
            kept = [p for p in held.split('+') if p not in self._baseline]
            held = '+'.join(kept) if kept else None
        if held == self._last_code:
            return None
        self._last_code = held
        if held is None:
            self._building = False
            return None
        if not self._building:
            if self.code is not None:
                if held in self._back_codes:
                    self._restart()
                    return None
                if held in self._confirm_codes:
                    self.accept()
                    return None
            self._building = True
            self._parts = []
        self._parts = _combo_grow(self._parts, held)
        self.code = '+'.join(self._parts)
        self._prompt.setText('Release when done, then Confirm, or press again.')
        _set_bind_visual(self._detected, self.code, '', _s(46), device=self._device)
        self._confirm.setEnabled(True)
        self._again.show()
        self._check_conflict()
        return None

    def _restart(self):
        self.code = None
        self._parts = []
        self._building = False
        self._detected.setText('')
        self._detected.setPixmap(QPixmap())
        self._prompt.setText(self._prompt_text)
        self._confirm.setEnabled(False)
        self._confirm.setText('Confirm')
        self._arm_confirm(False)
        self._warn.hide()
        self._again.hide()
        if self._kind != 'key':
            self._timer.start()
            return None
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None


class _RebindDialog(QDialog):
    """Controls window: pick the input device, then bind each action. DualSense
    keeps its touchpad scheme (only Lock Skip / Pause are buttons); Xbox and sim
    wheels bind every action to a button/hat; keyboard comes in a later stage."""

    _DEVICES = [('playstation', 'PlayStation'), ('xbox', 'Xbox'), ('keyboard', 'Keyboard'), ('wheel', 'Sim wheel')]
    _LOCK_TIP = 'Locks track-skipping. Handy in the garage - stay parked without the D-pad changing songs or fighting the radial menu.'
    _INTERACT_TIP = "The button you enter a race / car show / menu / house / Return Home with. Skip stays locked and music holds menu volume there until you drive off (those screens keep IsRaceOn=true, so telemetry can't see them)."
    _OPEN_TIP = 'HOLD this button about a second to bring Segue to the front; hold again to send it back to the tray. Shares the mic button with Lock Skip by default: tap = lock, hold = open.'
    _HOTKEY_TIP = 'Keyboard combo that opens/hides Segue no matter which input device is active. Works alongside the controller gesture. Click to rebind; bind nothing to disable.'
    _DS_FIXED = ['Skip track - D-pad Left / Right', 'Volume - touchpad swipe up / down', 'Pause - touchpad tap']
    def __init__(self, cfg, on_save, on_restart=None, ui=None, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._on_save = on_save
        self._on_restart = on_restart
        self._ui = ui
        self._orig_device = cfg.input_device
        self._wdev = cfg.input_device
        self._wds = {'safe_mode_button': cfg.safe_mode_button, 'pause_button': cfg.pause_button, 'skip_button': cfg.skip_button, 'open_button': getattr(cfg, 'open_button', 'micBtn'), 'latch_button': getattr(cfg, 'latch_button', 'share')}
        self._wbind_by_dev = {d: dict(b) for d, b in (getattr(cfg, 'bindings_by_device', {}) or {}).items()}
        self._whold_by_dev = {d: set(h) for d, h in (getattr(cfg, 'hold_actions_by_device', {}) or {}).items()}
        if self._wdev not in self._wbind_by_dev:
            self._wbind_by_dev[self._wdev] = dict(cfg.bindings)
        if self._wdev not in self._whold_by_dev:
            self._whold_by_dev[self._wdev] = set(getattr(cfg, 'hold_actions', []) or [])
        self._wbind = dict(self._wbind_by_dev[self._wdev])
        self._whold = set(self._whold_by_dev[self._wdev])
        self._orig_bind_by_dev = {d: dict(b) for d, b in self._wbind_by_dev.items()}
        self._orig_hold_by_dev = {d: set(h) for d, h in self._whold_by_dev.items()}
        self._orig_opentrig = _ib.open_trigger_for(cfg)
        self._wopentrig = self._orig_opentrig
        self._opentrig_touched = False
        self._orig_hotkey = getattr(cfg, 'open_hotkey', '')
        self._whotkey = self._orig_hotkey
        self._more_open = False
        if cfg.skip_button and (not cfg.gamepad_skip_enabled) and (not cfg.touchpad_skip_enabled):
            self._orig_skip = 'lspress'
        elif not cfg.gamepad_skip_enabled and cfg.touchpad_skip_enabled:
            self._orig_skip = 'touchpad'
        else:
            self._orig_skip = 'dpad'
        self._wskip = self._orig_skip
        self._orig_pause = cfg.pause_input if cfg.pause_input in ('tap', 'press') else 'tap'
        self._wpause = self._orig_pause
        self._orig_tapsens = int(getattr(cfg, 'tap_sensitivity', 70))
        self._wtapsens = self._orig_tapsens
        self._orig_volsens = int(round(float(getattr(cfg, 'vol_hold_sensitivity', 1.0)) * 100))
        self._wvolsens = self._orig_volsens
        self.setWindowTitle('Controls')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        from PySide6.QtGui import QPalette
        _pal = self.palette()
        _pal.setColor(QPalette.Window, QColor(_c('sunk')))
        self.setPalette(_pal)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setFont(_ui_font(15))
        self.setMinimumWidth(_s(440))
        root = QVBoxLayout(self)
        root.setContentsMargins(_s(18), _s(16), _s(18), _s(16))
        root.setSpacing(_s(10))
        root.addWidget(self._hdr('Select Device'))
        devrow = QHBoxLayout()
        devrow.setSpacing(_s(6))
        self._dev_btns = {}
        from fh6_spotify import game_presets as _gp
        _allowed = set(_gp.supported_devices(self._cfg.game_preset))
        _allowed.add(self._wdev)
        for dev, label in [(d, l) for d, l in self._DEVICES if d in _allowed]:
            tag = 'Best' if dev == 'playstation' else 'Beta' if dev in ('xbox', 'wheel') else ''
            b = _DeviceButton(tag)
            b.setObjectName('devbtn')
            _ic = 42 if dev == 'keyboard' else 30 if dev == 'wheel' else 34
            pm = _dev_pixmap(dev, _ic) or _load_scaled(os.path.join(_ASSETS, dev + '.png'), _ic)
            if pm is None and dev == 'playstation':
                pm = _load_scaled(os.path.join(_ASSETS, 'dualsense.png'), _ic)
            if pm is None:
                pm = _dev_icon(dev, _ic).pixmap(QSize(_s(_ic), _s(_ic)))
            if dev == 'playstation':
                label = 'PlayStation - recommended (DualSense or DualShock 4, touchpad)'
            elif dev == 'wheel':
                label = 'Sim wheel - beta'
            b.setToolTip(label)
            b.setIcon(QIcon(pm))
            b.setIconSize(QSize(_s(_ic), _s(_ic)))
            b.setMinimumHeight(_s(50))
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(dev == cfg.input_device)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, d=dev: self._set_device(d))
            devrow.addWidget(b, 1)
            self._dev_btns[dev] = b
        root.addLayout(devrow)
        line = QFrame()
        line.setObjectName('vline')
        line.setFixedHeight(1)
        root.addWidget(line)
        self._content = QWidget()
        self._content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._cl = QVBoxLayout(self._content)
        self._cl.setContentsMargins(0, 0, _s(14), 0)
        self._cl.setSpacing(_s(8))
        self._scroll = _FadeScroll()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)
        self._save_btn = QPushButton('Save')
        self._save_btn.setObjectName('savebtn')
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(self._commit)
        self._save_btn.hide()
        root.addWidget(self._save_btn)
        self._rebuild_content()
        self._update_save_btn()
        return None
    def _hdr(self, text):
        l = QLabel(text)
        l.setFont(_ui_font(15, QFont.Bold))
        return l

    def _hdr_with_info(self, text, factory):
        """Section header with a circled-i that hover-shows `factory()` (e.g. the
        gesture GIFs) - keeps the clips out of the main window."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_s(6))
        row.addWidget(self._hdr(text))
        row.addWidget(_InfoPopupLabel(factory), 0, Qt.AlignVCenter)
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        return w

    def _info_line(self, text):
        l = QLabel(text)
        l.setObjectName('hint')
        l.setFont(_ui_font(14))
        l.setWordWrap(True)
        return l

    def _hdr_with_reset(self, text):
        """Section header with a flat 'Reset to defaults' link on the right."""
        row = QHBoxLayout()
        row.addWidget(self._hdr(text))
        row.addStretch(1)
        reset = QPushButton('↺  Reset to defaults')
        reset.setObjectName('advbtn')
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(self._reset_binds)
        self._reset_btn = reset
        row.addWidget(reset)
        reset.setVisible(False)
        return row

    def _has_custom_binds(self) -> bool:
        """True if the current device's binds differ from factory defaults
        (so the Reset link is worth showing)."""
        if self._wbind:
            return True
        from fh6_spotify.config import Config
        fresh = Config()
        return self._wds.get('safe_mode_button') != fresh.safe_mode_button or self._wds.get('pause_button') != fresh.pause_button or self._wds.get('skip_button') != fresh.skip_button or self._wds.get('open_button') != fresh.open_button

    def _set_device(self, dev):
        if dev == self._wdev:
            for d, b in self._dev_btns.items():
                b.setChecked(d == dev)
            return None
        for d, b in self._dev_btns.items():
            b.setChecked(d == dev)
        self._wbind_by_dev[self._wdev] = dict(self._wbind)
        self._whold_by_dev[self._wdev] = set(self._whold)
        self._wdev = dev
        self._wbind = dict(self._wbind_by_dev.get(dev, {}))
        self._whold = set(self._whold_by_dev.get(dev, set()))
        if not self._opentrig_touched and getattr(self._cfg, 'open_trigger', '') in ('hold', 'press'):
            self._wopentrig = 'press' if dev == 'keyboard' else 'hold'
        self._rebuild_content()
        self._update_save_btn()
        return None
    def _wheel_preset_row(self):
        """Sim-wheel preset picker - per-wheel presets (G29, Fanatec, etc.)
        aren't built yet, so this is a disabled 'Coming soon' teaser styled like
        the mic-device dropdown."""
        box = QVBoxLayout()
        box.setSpacing(_s(4))
        box.addWidget(self._hdr('Wheel preset'))
        combo = QComboBox()
        combo.addItem('Coming soon')
        combo.setEnabled(False)
        combo.setToolTip('Per-wheel presets (G29, Fanatec, etc.) are coming soon.')
        box.addWidget(combo)
        return box

    def _rebuild_content(self):
        _clear_layout(self._cl)
        self._vh_row = None
        dev = self._wdev
        if dev in ('playstation', 'dualsense', 'dualshock'):
            is_forza_preset = self._cfg.game_preset == 'forza'
            self._cl.addLayout(self._hdr_with_reset('Bindings'))
            _sw_row = QHBoxLayout()
            _sw_row.setContentsMargins(0, 0, 0, 0)
            _sw_row.setSpacing(_s(6))
            _sw_lbl = QLabel('Skip with')
            _sw_lbl.setFont(_ui_font(14))
            _sw_row.addWidget(_sw_lbl)
            _sw_row.addWidget(_InfoPopupLabel(self._fixed_controls_clips), 0, Qt.AlignVCenter)
            _sw_row.addStretch(1)
            _sw_row.addLayout(self._skip_input_row(trailing_stretch=False))
            self._cl.addLayout(_sw_row)
            if is_forza_preset:
                self._cl.addLayout(self._ds_row('Pause / play', 'pause_button', 'Touchpad tap', ''))
            else:
                _tp_row = QHBoxLayout()
                _tp_row.setContentsMargins(0, 0, 0, 0)
                _tp_row.setSpacing(_s(6))
                _tp_lbl = QLabel('Touchpad Pause')
                _tp_lbl.setFont(_ui_font(14))
                _tp_row.addWidget(_tp_lbl)
                _tp_row.addWidget(_InfoPopupLabel(lambda: self._fixed_controls_clips('pause')), 0, Qt.AlignVCenter)
                _tp_row.addStretch(1)
                _tp_row.addLayout(self._pause_input_row(trailing_stretch=False))
                self._cl.addLayout(_tp_row)
            if is_forza_preset and (not self._wds.get('pause_button')) or (not is_forza_preset and self._wpause == 'tap'):
                _tap_row = QWidget()
                _tap_row.setToolTip("How easily a touchpad tap pauses/plays.\n0 = off (no tap-to-pause; use a bound button, or touchpad click where available).\nHigher = lighter/quicker taps trigger (hair-trigger).\nLower = needs a firmer, deliberate tap, so light grazes and volume swipes don't accidentally pause.")
                _tv = QVBoxLayout(_tap_row)
                _tv.setContentsMargins(0, 0, 0, 0)
                _tv.setSpacing(_s(4))
                _th = QHBoxLayout()
                _th.setSpacing(_s(6))
                _tlbl = QLabel('Touchpad tap sensitivity')
                _tlbl.setFont(_ui_font(15))
                _tval = QLabel('Off' if self._wtapsens <= 0 else f'{self._wtapsens}%')
                _tval.setObjectName('hint')
                _tval.setFont(_ui_font(13))
                _th.addWidget(_tlbl)
                _th.addStretch(1)
                _th.addWidget(_tval)
                _tv.addLayout(_th)
                _tsl = QSlider(Qt.Horizontal)
                _tsl.setRange(0, 100)
                _tsl.setValue(self._wtapsens)
                _tsl.setCursor(Qt.PointingHandCursor)
                _tsl.valueChanged.connect(lambda x, lbl=_tval: (lbl.setText('Off' if x <= 0 else f'{x}%'), self._on_tap_sensitivity(x)))
                _tv.addWidget(_tsl)
                self._cl.addWidget(_tap_row)
            if is_forza_preset:
                self._cl.addLayout(self._ds_row('Interact button', 'latch_button', 'Square', self._INTERACT_TIP))

            def _ps_more(v):
                v.addLayout(self._ds_row('Lock Skip', 'safe_mode_button', '', self._LOCK_TIP))
                v.addLayout(self._ds_row('Open Segue', 'open_button', '', self._OPEN_TIP, suffix='(hold)'))
                v.addLayout(self._hotkey_row())
                return None
            self._more_toggle(_ps_more)
        elif dev in ('xbox', 'wheel', 'keyboard'):
            is_forza_preset = self._cfg.game_preset == 'forza'
            if dev == 'wheel':
                self._cl.addLayout(self._wheel_preset_row())
            self._cl.addLayout(self._hdr_with_reset('Bindings'))
            for action in ('prev', 'next', 'vol_down', 'vol_up', 'pause'):
                self._cl.addLayout(self._action_row(action))
            if dev in ('xbox', 'wheel'):
                _vh_row = QWidget(self._content)
                _vh_row.setToolTip("How fast the volume sweeps while you HOLD volume up/down.\n100% = default speed. Higher = faster sweep, lower = slower, finer control.\nDoes not change how long a hold takes to register (your tap-to-skip stays safe) or the single-tap step.")
                _vhv = QVBoxLayout(_vh_row)
                _vhv.setContentsMargins(0, 0, 0, 0)
                _vhv.setSpacing(_s(4))
                _vhh = QHBoxLayout()
                _vhh.setSpacing(_s(6))
                _vhlbl = QLabel('Volume hold speed')
                _vhlbl.setFont(_ui_font(15))
                _vhval = QLabel(f'{self._wvolsens}%')
                _vhval.setObjectName('hint')
                _vhval.setFont(_ui_font(13))
                _vhh.addWidget(_vhlbl)
                _vhh.addStretch(1)
                _vhh.addWidget(_vhval)
                _vhv.addLayout(_vhh)
                _vhsl = QSlider(Qt.Horizontal)
                _vhsl.setRange(50, 200)
                _vhsl.setValue(self._wvolsens)
                _vhsl.setCursor(Qt.PointingHandCursor)
                _vhsl.valueChanged.connect(lambda x, lbl=_vhval: (lbl.setText(f'{x}%'), self._on_vol_hold_sensitivity(x)))
                _vhv.addWidget(_vhsl)
                self._cl.addWidget(_vh_row)
                self._vh_row = _vh_row
                _vh_row.setEnabled(self._vol_hold_active())
            if is_forza_preset:
                self._cl.addLayout(self._action_row('menu_latch'))

            def _gen_more(v):
                actions = ('safe_mode',) if dev == 'keyboard' else ('safe_mode', 'open')
                for action in actions:
                    v.addLayout(self._action_row(action))
                v.addLayout(self._hotkey_row(label='Open Segue' if dev == 'keyboard' else 'Open Segue (keyboard)'))
                return None
            self._more_toggle(_gen_more)
            if dev == 'wheel':
                self._cl.addWidget(self._info_line("Sim wheel is untested. I don't own one, so let's just pray it works 🙏"))
            if dev == 'keyboard':
                self._cl.addWidget(self._info_line("Safe defaults that don't clash with most games. Click any key-cap to rebind it."))
        self._cl.addStretch(1)
        if getattr(self, '_reset_btn', None) is not None:
            self._reset_btn.setVisible(self._has_custom_binds())
        self._fit_soon()
        return None
    def _more_toggle(self, build) -> None:
        """Collapsible "More options" block. `build(v)` fills a QVBoxLayout
        with the advanced rows; one flat toggle shows/hides them so the
        dialog stays lean. Expansion state is session-only (re-applied across
        rebuilds, reset when the dialog reopens)."""
        btn = QPushButton('More options')
        _style_expander(btn, lambda: self._more_open)
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, _s(2), 0, 0)
        v.setSpacing(self._cl.spacing())
        build(v)
        wrap.setVisible(self._more_open)

        def _tgl(_=False):
            self._more_open = not self._more_open
            wrap.setVisible(self._more_open)
            btn.update()
            self._fit_soon()
            return None
        btn.clicked.connect(_tgl)
        self._cl.addWidget(btn)
        self._cl.addWidget(wrap)
        return None
    def _fit_soon(self) -> None:
        """Resize-to-content with the two-pass dance: the first fit measures
        a partially-settled layout (left ~65px of dead space after collapses
        AND device switches), the second - chained on the NEXT event-loop
        tick, not a fixed delay - converges within a frame, so no visible
        two-step resize. Use this everywhere instead of calling
        _fit_to_content directly after layout changes."""

        def _p():
            self._fit_to_content()
            QTimer.singleShot(0, self._fit_to_content)
        QTimer.singleShot(0, _p)
        return None

    def _fit_to_content(self) -> None:
        self.setMaximumHeight(16777215)

        def _refresh(lay):
            if lay is None:
                return None
            lay.invalidate()
            for i in range(lay.count()):
                it = lay.itemAt(i)
                w = it.widget()
                if w is not None:
                    w.updateGeometry()
                    if w.layout() is not None:
                        _refresh(w.layout())
                elif it.layout() is not None:
                    _refresh(it.layout())
            return None
        _refresh(self._cl)
        self._cl.activate()
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self._content.updateGeometry()
        content_h = self._content.sizeHint().height()
        self._scroll.setMinimumHeight(content_h)
        hint = self.sizeHint()
        chrome = hint.height() - content_h
        scr = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        cap_content = max(_s(220), int(scr.height() * 0.92) - chrome)
        extra_w = 0
        if content_h > cap_content:
            self._scroll.setMinimumHeight(cap_content)
            hint = self.sizeHint()
            extra_w = 0
        self.setMaximumHeight(hint.height())
        self.resize(hint.width() + extra_w, hint.height())
        g = self.frameGeometry()
        if g.bottom() > scr.bottom():
            self.move(g.x(), max(scr.top(), scr.bottom() - g.height()))
            return None
        return None

    def _skip_options(self):
        if self._cfg.mode == 'general':
            return [('touchpad', 'Touchpad swipe'), ('lspress', 'Left stick press')]
        return [('dpad', 'D-pad'), ('touchpad', 'Touchpad swipe')]

    def _skip_input_row(self, trailing_stretch=True):
        """Mutually-exclusive skip-input pills. Options come from
        _skip_options (preset-aware). Working state only - commits on Save.
        trailing_stretch=False leaves the pills flush-right for a header row
        that already supplies the left-side stretch."""
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        self._skip_btns = {}
        valid_keys = [k for k, _ in self._skip_options()]
        if self._wskip not in valid_keys:
            self._wskip = valid_keys[0]
        for key, label in self._skip_options():
            btn = _SkipModeButton(label)
            btn.setObjectName('skipbtn')
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(self._wskip == key)
            btn.setProperty('active', self._wskip == key)
            btn.clicked.connect(lambda _=False, k=key: self._set_skip_input(k))
            self._skip_btns[key] = btn
            row.addWidget(btn)
        if trailing_stretch:
            row.addStretch(1)
        return row

    def _set_skip_input(self, key: str) -> None:
        if key == self._wskip:
            return None
        self._wskip = key
        for k, btn in self._skip_btns.items():
            on = key == k
            btn.setChecked(on)
            btn.setProperty('active', on)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self._update_save_btn()
        self._rebuild_content()
        return None

    def _pause_input_row(self, trailing_stretch=True):
        """Tap / Press toggle. Tap = light touchpad tap (existing). Press =
        the clickable touchpad button. Mutually exclusive. Working state
        only - commits via the bottom Save button, same as the skip toggle.
        trailing_stretch=False leaves the pills flush-right for a header row
        that already supplies the left-side stretch."""
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        self._pause_tap_btn = _SkipModeButton('Tap')
        self._pause_press_btn = _SkipModeButton('Press')
        for btn, key in ((self._pause_tap_btn, 'tap'), (self._pause_press_btn, 'press')):
            btn.setObjectName('skipbtn')
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(self._wpause == key)
            btn.setProperty('active', self._wpause == key)
            btn.clicked.connect(lambda _=False, k=key: self._set_pause_input(k))
        row.addWidget(self._pause_tap_btn)
        row.addWidget(self._pause_press_btn)
        if trailing_stretch:
            row.addStretch(1)
        return row

    def _hotkey_row(self, label='Open Segue (keyboard)'):
        """Global Open-Segue keyboard hotkey row - works on EVERY device.
        On the keyboard card it IS the Open Segue bind (plain label); pads
        and wheel carry the "(keyboard)" qualifier next to their controller
        bind. Independent bind space (cfg.open_hotkey), captured with the
        keyboard popup regardless of the selected device."""
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        name = QLabel(label)
        name.setFont(_ui_font(14))
        row.addWidget(name)
        row.addWidget(_InfoLabel(self._HOTKEY_TIP, 15))
        cap = _KeycapButton(_pretty_code(self._whotkey) if self._whotkey else 'Unbound')
        cap.setToolTip('Click to rebind')
        cap.clicked.connect(lambda _=False, c=cap: self._do_hotkey(c))
        row.addStretch(1)
        row.addWidget(cap)
        return row

    def _do_hotkey(self, cap):
        ctrl = (self._ui or {}).get('controller')
        if ctrl is not None:
            try:
                ctrl._suppressed = True
            except Exception:
                pass
        try:
            dlg = _CaptureDialog(None, self, kind='key', owner_of=None, device='keyboard')
            if dlg.exec() != QDialog.Accepted or dlg.code is None:
                return None
            self._whotkey = dlg.code
        finally:
            if ctrl is not None:
                try:
                    ctrl._suppressed = False
                except Exception:
                    pass
        cap.setText(_pretty_code(self._whotkey) if self._whotkey else 'Unbound')
        self._update_save_btn()
        return None

    def _open_trigger_row(self):
        """Hold / Press toggle for the Open Segue bind (keyboard/xbox/wheel).
        Compact sub-row under the bind: dim 'Trigger' label + the two pills.
        Working state only - commits via the bottom Save button."""
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        _tl = QLabel('Trigger')
        _tl.setObjectName('hint')
        _tl.setFont(_ui_font(13))
        row.addWidget(_tl)
        self._opentrig_hold_btn = _SkipModeButton('Hold')
        self._opentrig_press_btn = _SkipModeButton('Press')
        for btn, key in ((self._opentrig_hold_btn, 'hold'), (self._opentrig_press_btn, 'press')):
            btn.setObjectName('skipbtn')
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(self._wopentrig == key)
            btn.setProperty('active', self._wopentrig == key)
            btn.clicked.connect(lambda _=False, k=key: self._set_open_trigger(k))
        row.addWidget(self._opentrig_hold_btn)
        row.addWidget(self._opentrig_press_btn)
        row.addStretch(1)
        return row
    def _set_open_trigger(self, key: str) -> None:
        if key == self._wopentrig:
            return None
        self._wopentrig = key
        self._opentrig_touched = True
        for btn, k in ((self._opentrig_hold_btn, 'hold'), (self._opentrig_press_btn, 'press')):
            on = key == k
            btn.setChecked(on)
            btn.setProperty('active', on)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self._update_save_btn()
        return None

    def _set_pause_input(self, key: str) -> None:
        if key == self._wpause:
            return None
        self._wpause = key
        for btn, k in ((self._pause_tap_btn, 'tap'), (self._pause_press_btn, 'press')):
            on = key == k
            btn.setChecked(on)
            btn.setProperty('active', on)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self._update_save_btn()
        self._rebuild_content()
        return None
    def _on_tap_sensitivity(self, x: int) -> None:
        self._wtapsens = int(x)
        self._update_save_btn()
        return None

    def _on_vol_hold_sensitivity(self, x: int) -> None:
        self._wvolsens = int(x)
        self._update_save_btn()
        return None

    def _vol_hold_active(self) -> bool:
        return 'vol_up' in self._whold or 'vol_down' in self._whold

    def _refresh_vol_hold_enabled(self) -> None:
        if getattr(self, '_vh_row', None) is not None:
            self._vh_row.setEnabled(self._vol_hold_active())
            return None
        return None

    def _fixed_controls_clips(self, kind: str = 'skip') -> QWidget:
        """Single-row mini GIFs with two-tier labels above each: big bold
        action title + dimmer subtitle (the gesture) + the looping clip.
        kind="skip" shows only the skip gesture(s) (for the Skip-with info);
        kind="pause" shows Volume + Pause/Play (for the Touchpad-Pause info)."""
        from PySide6.QtGui import QImageReader
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setSpacing(_s(8))
        row.setContentsMargins(0, 0, 0, 0)
        pause_sub = 'Press' if self._wpause == 'press' else 'Tap'
        pause_clip = 'clips/touchpad_press.gif' if self._wpause == 'press' else 'clips/touchpad_tap.gif'
        if not os.path.exists(os.path.join(_ASSETS, pause_clip)):
            pause_clip = 'clips/touchpad_tap.gif'
        if kind == 'pause':
            clips = [('Volume', 'Swipe up / down', 'clips/touchpad_swipe_up.gif'), ('Pause / Play', pause_sub, pause_clip)]
            target_w = _s(115)
        elif self._wskip == 'touchpad':
            clips = [('Next', 'Swipe right', 'clips/touchpad_swipe_right.gif'), ('Previous', 'Swipe left', 'clips/touchpad_swipe_left.gif')]
            target_w = _s(115)
        elif self._wskip == 'lspress':
            note = QLabel('Click the left stick (L3) to skip tracks.')
            note.setFont(_ui_font(12))
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {_c('text_dim')};")
            row.addWidget(note)
            return wrap
        else:
            clips = [('Skip', 'D-pad Left / Right', 'clips/dpad_press_right_left.gif')]
            target_w = _s(140)
        for title, subtitle, name in clips:
            p = os.path.join(_ASSETS, name)
            if not os.path.exists(p):
                continue
            src = QImageReader(p).size()
            if src.isValid() and src.width() > 0:
                target_h = int(target_w * src.height() / src.width())
            else:
                target_h = int(target_w * 0.6)
            cell = QWidget()
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(0, 0, 0, 0)
            cv.setSpacing(_s(1))
            narrow = target_w < _s(100)
            t_lbl = QLabel(title)
            t_lbl.setFont(_ui_font(11 if narrow else 13, QFont.Bold))
            t_lbl.setStyleSheet(f"color: {_c('text')};")
            t_lbl.setWordWrap(True)
            cv.addWidget(t_lbl)
            sub_lbl = QLabel(subtitle)
            sub_lbl.setFont(_ui_font(9 if narrow else 10))
            sub_lbl.setStyleSheet(f"color: {_c('text_hint')};")
            sub_lbl.setWordWrap(True)
            cv.addWidget(sub_lbl)
            cv.addSpacing(_s(3))
            lbl = _RoundedMovieLabel(_s(6))
            movie = QMovie(p)
            movie.setScaledSize(QSize(target_w, target_h))
            lbl.setMovie(movie)
            movie.start()
            lbl.setFixedSize(QSize(target_w, target_h))
            cv.addWidget(lbl)
            cell.setFixedWidth(target_w)
            row.addWidget(cell, 0, Qt.AlignTop)
        row.addStretch(1)
        return wrap
    def _single_clip_cell(self, title: str, subtitle: str, clip_name: str) -> QWidget:
        """One title + subtitle + GIF cell, left-aligned, for Xbox/wheel."""
        from PySide6.QtGui import QImageReader
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        p_clip = os.path.join(_ASSETS, clip_name)
        if not os.path.exists(p_clip):
            return wrap
        src = QImageReader(p_clip).size()
        target_w = _s(140)
        if src.isValid() and src.width() > 0:
            target_h = int(target_w * src.height() / src.width())
        else:
            target_h = int(target_w * 0.6)
        cell = QWidget()
        cv = QVBoxLayout(cell)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(_s(1))
        t_lbl = QLabel(title)
        t_lbl.setFont(_ui_font(13, QFont.Bold))
        t_lbl.setStyleSheet(f"color: {_c('text')};")
        cv.addWidget(t_lbl)
        sub_lbl = QLabel(subtitle)
        sub_lbl.setFont(_ui_font(10))
        sub_lbl.setStyleSheet(f"color: {_c('text_hint')};")
        cv.addWidget(sub_lbl)
        cv.addSpacing(_s(3))
        lbl = _RoundedMovieLabel(_s(6))
        movie = QMovie(p_clip)
        movie.setScaledSize(QSize(target_w, target_h))
        lbl.setMovie(movie)
        movie.start()
        lbl.setFixedSize(QSize(target_w, target_h))
        cv.addWidget(lbl)
        cell.setFixedWidth(target_w)
        row.addWidget(cell, 0, Qt.AlignTop)
        row.addStretch(1)
        return wrap

    def _ds_row(self, label, attr, fallback, tip, suffix=''):
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        name = QLabel(label)
        name.setFont(_ui_font(14))
        row.addWidget(name)
        if tip:
            row.addWidget(_InfoLabel(tip, 15))
        cap = _KeycapButton('')
        _set_bind_visual(cap, self._wds[attr], fallback, _s(22), suffix=suffix)
        cap.setToolTip('Click to rebind')
        cap.clicked.connect(lambda _=False, a=attr, c=cap, f=fallback, sx=suffix: self._do_ds(a, c, f, suffix=sx))
        row.addStretch(1)
        row.addWidget(cap)
        return row

    _HOLDABLE = {'pause', 'prev', 'vol_up', 'vol_down', 'safe_mode', 'next'}

    def _action_row(self, action):
        """Binding row: action label + a clickable key-cap showing the current
        key/button. Click the cap to rebind (no separate Rebind button).
        Xbox/wheel holdable actions also get a Tap/Hold pill so one button can do
        tap=skip + hold=volume."""
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        name = QLabel(_ib.ACTION_LABELS[action])
        name.setFont(_ui_font(14))
        row.addWidget(name)
        if action == 'safe_mode':
            row.addWidget(_InfoLabel(self._LOCK_TIP, 15))
        elif action == 'menu_latch':
            row.addWidget(_InfoLabel(self._INTERACT_TIP, 15))
        eff = _ib.effective_bindings(self._wdev, self._wbind, getattr(self._cfg, 'mode', None))
        cap = _KeycapButton('')
        _set_bind_visual(cap, eff.get(action), px=_s(22), device=self._wdev)
        cap.setToolTip('Click to rebind')
        cap.clicked.connect(lambda _=False, a=action, c=cap: self._do_action(a, c))
        row.addStretch(1)
        if self._wdev in ('xbox', 'wheel') and action in self._HOLDABLE:
            row.addWidget(self._trigger_pill(action))
        row.addWidget(cap)
        return row

    def _trigger_pill(self, action):
        """Small Tap/Hold toggle for a bind. Hold = the action fires only after
        the button is held ~0.3s, freeing a tap of the same button for a second
        action (e.g. D-pad Right tap = skip, hold = volume up)."""
        pill = QPushButton('Hold' if action in self._whold else 'Tap')
        pill.setObjectName('trigpill')
        pill.setCursor(Qt.PointingHandCursor)
        pill.setFont(_ui_font(12))
        pill.setToolTip('Tap = fires on press (normal).\nHold = fires only when held ~0.3s.\nSet one action to Tap and another to Hold on the SAME button to do both, e.g. D-pad Right tap = skip, hold = volume up.')

        def _flip(_=False):
            if action in self._whold:
                self._whold.discard(action)
            else:
                self._whold.add(action)
            pill.setText('Hold' if action in self._whold else 'Tap')
            self._style_trigpill(pill, action)
            if action in ('vol_up', 'vol_down'):
                self._refresh_vol_hold_enabled()
            self._update_save_btn()
            return None
        pill.clicked.connect(_flip)
        self._style_trigpill(pill, action)
        return pill

    def _style_trigpill(self, pill, action):
        on = action in self._whold
        pill.setStyleSheet(f"QPushButton#trigpill {{ color: {_c('text') if on else _c('text_dim')}; border: 1px solid {_ACCENT if on else _c('border')}; border-radius: {_s(6)}px; padding: {_s(2)}px {_s(9)}px; font-weight: {'700' if on else '500'}; background: {'rgba(167,159,138,0.16)' if on else 'transparent'}; }}QPushButton#trigpill:hover {{ border-color: {_ACCENT}; }}")
        return None

    _DS_LABELS = {
        'safe_mode_button': 'Lock Skip',
        'pause_button': 'Pause / play',
        'skip_button': 'Skip',
        'open_button': 'Open Segue',
        'latch_button': 'Interact button',
    }
    _SHARE_OK = {('open_button', 'safe_mode_button'), ('safe_mode_button', 'open_button')}

    def _bind_owner(self, code, skip_action=None, skip_attr=None):
        """Human label of whatever already uses `code` on the current device,
        excluding the row being rebound, or None. A code matching a device
        DEFAULT counts (it's a real runtime conflict)."""
        if not code:
            return None
        eff = _ib.effective_bindings(self._wdev, self._wbind, getattr(self._cfg, 'mode', None))
        for a, c in eff.items():
            if c != code or a == skip_action:
                continue
            if self._wdev in ('xbox', 'wheel') and skip_action in self._HOLDABLE and a in self._HOLDABLE:
                continue
            return _ib.ACTION_LABELS.get(a, a)
        for attr, lbl in self._DS_LABELS.items():
            if attr == skip_attr or (skip_attr, attr) in self._SHARE_OK:
                continue
            if self._wds.get(attr) == code:
                return lbl
        return None

    def _clear_owner(self, code, skip_action=None, skip_attr=None):
        """Unbind whoever currently holds `code` so a confirmed rebind MOVES it.
        Defaults are overridden to "" (explicit unbound) in the working copy."""
        if not code:
            return None
        eff = _ib.effective_bindings(self._wdev, self._wbind, getattr(self._cfg, 'mode', None))
        for a, c in eff.items():
            if c != code or a == skip_action:
                continue
            if self._wdev in ('xbox', 'wheel') and skip_action in self._HOLDABLE and a in self._HOLDABLE:
                continue
            self._wbind[a] = ''
        for attr in self._DS_LABELS:
            if attr == skip_attr or (skip_attr, attr) in self._SHARE_OK:
                continue
            if self._wds.get(attr) == code:
                self._wds[attr] = ''
        return None

    def _capture(self, owner_of=None):
        """Suppress live binds while the capture popup is open (so pressing a
        bound input to rebind it doesn't also fire its action), then capture."""
        ctrl = (self._ui or {}).get('controller')
        if ctrl is not None:
            try:
                ctrl._suppressed = True
            except Exception:
                pass
        try:
            return self._capture_run(owner_of)
        finally:
            if ctrl is not None:
                try:
                    ctrl._suppressed = False
                except Exception:
                    pass

    def _capture_run(self, owner_of=None):
        """Open the capture popup; return the chosen code, or None.

        Keyboard captures keys via Qt key events (no live backend needed), so
        it works right after switching to keyboard - before saving. Controller
        capture polls the live backend, which only matches the picked device once
        it's committed via Save (the device then hot-swaps in live) - so that path
        keeps the 'save first' guard. `owner_of` drives the conflict warning."""
        if self._wdev == 'keyboard':
            dlg = _CaptureDialog(None, self, kind='key', owner_of=owner_of, device=self._wdev)
            if dlg.exec() == QDialog.Accepted and dlg.code is not None:
                return dlg.code
            return None
        if self._wdev != self._orig_device:
            QMessageBox.information(self, 'Save first', 'Save the device switch first, then rebind its buttons.')
            return None
        reader = (self._ui or {}).get('controller')
        if reader is None or not hasattr(reader, 'read_pressed'):
            QMessageBox.information(self, 'No controller', 'No controller detected.')
            return None
        dlg = _CaptureDialog(reader, self, kind='button', owner_of=owner_of, device=self._wdev)
        if dlg.exec() == QDialog.Accepted and dlg.code is not None:
            return dlg.code
        return None

    def _do_ds(self, attr, cur_label, fallback, suffix=''):
        code = self._capture(owner_of=lambda c: self._bind_owner(c, skip_attr=attr))
        if code is None:
            return None
        moved = bool(code) and self._bind_owner(code, skip_attr=attr) is not None
        if code:
            self._clear_owner(code, skip_attr=attr)
        self._wds[attr] = code
        if moved or attr == 'pause_button':
            self._rebuild_content()
        else:
            _set_bind_visual(cur_label, code, fallback, _s(22), suffix=suffix)
        self._update_save_btn()
        return None
    def _reset_binds(self):
        """Wipe the current device's binds back to defaults: action bindings
        clear to the device defaults, and the DualSense Lock Skip / Pause
        buttons return to their factory values. Working state only - the user
        still has to press Save to commit."""
        from fh6_spotify.config import Config
        fresh = Config()
        self._wbind = {}
        self._whold = set()
        self._wds = {'safe_mode_button': fresh.safe_mode_button, 'pause_button': fresh.pause_button, 'skip_button': fresh.skip_button, 'open_button': fresh.open_button, 'latch_button': fresh.latch_button}
        self._rebuild_content()
        self._update_save_btn()
        return None

    def _do_action(self, action, cur_label):
        code = self._capture(owner_of=lambda c: self._bind_owner(c, skip_action=action))
        if code is None:
            return None
        moved = bool(code) and self._bind_owner(code, skip_action=action) is not None
        if code:
            self._clear_owner(code, skip_action=action)
        self._wbind[action] = code
        if moved:
            self._rebuild_content()
        else:
            _set_bind_visual(cur_label, code, px=_s(22), device=self._wdev)
        self._update_save_btn()
        return None

    def _cur_bind_map(self):
        """Current per-device binds with the active device's live edits folded
        in, empties dropped (a device with no overrides == absent)."""
        m = {d: dict(b) for d, b in self._wbind_by_dev.items()}
        m[self._wdev] = dict(self._wbind)
        return {d: b for d, b in m.items() if b}

    def _cur_hold_map(self):
        m = {d: set(h) for d, h in self._whold_by_dev.items()}
        m[self._wdev] = set(self._whold)
        return {d: h for d, h in m.items() if h}

    def _binds_dirty(self) -> bool:
        return self._wds['safe_mode_button'] != self._cfg.safe_mode_button or self._wds['pause_button'] != self._cfg.pause_button or self._wds['open_button'] != getattr(self._cfg, 'open_button', 'micBtn') or self._wds['latch_button'] != getattr(self._cfg, 'latch_button', 'share') or self._cur_bind_map() != {d: b for d, b in self._orig_bind_by_dev.items() if b} or self._cur_hold_map() != {d: h for d, h in self._orig_hold_by_dev.items() if h} or self._wskip != self._orig_skip or self._wpause != self._orig_pause or self._wopentrig != self._orig_opentrig or self._whotkey != self._orig_hotkey or self._wtapsens != self._orig_tapsens or self._wvolsens != self._orig_volsens

    def _update_save_btn(self):
        self._save_btn.show()
        self._save_btn.setText('Save')
        self._save_btn.setEnabled(self._wdev != self._orig_device or self._binds_dirty())
        if getattr(self, '_reset_btn', None) is not None:
            self._reset_btn.setVisible(self._has_custom_binds())
            return None
        return None
    def _commit(self):
        restart = self._wdev != self._orig_device
        self._cfg.input_device = self._wdev
        self._cfg.safe_mode_button = self._wds['safe_mode_button']
        self._cfg.pause_button = self._wds['pause_button']
        self._cfg.open_button = self._wds['open_button']
        self._cfg.latch_button = self._wds['latch_button']
        if self._opentrig_touched or getattr(self._cfg, 'open_trigger', '') in ('hold', 'press'):
            self._cfg.open_trigger = self._wopentrig
        self._cfg.open_hotkey = self._whotkey
        self._wbind_by_dev[self._wdev] = dict(self._wbind)
        self._whold_by_dev[self._wdev] = set(self._whold)
        self._cfg.bindings_by_device = {d: dict(b) for d, b in self._wbind_by_dev.items() if b}
        self._cfg.hold_actions_by_device = {d: sorted(h) for d, h in self._whold_by_dev.items() if h}
        self._cfg.bindings = dict(self._wbind)
        self._cfg.hold_actions = sorted(self._whold)
        if self._wskip == 'touchpad':
            self._cfg.gamepad_skip_enabled = False
            self._cfg.touchpad_skip_enabled = True
            self._cfg.skip_button = ''
        elif self._wskip == 'lspress':
            self._cfg.gamepad_skip_enabled = False
            self._cfg.touchpad_skip_enabled = False
            self._cfg.skip_button = 'L3'
        else:
            self._cfg.gamepad_skip_enabled = True
            self._cfg.touchpad_skip_enabled = False
            self._cfg.skip_button = ''
        self._cfg.pause_input = self._wpause
        self._cfg.tap_sensitivity = self._wtapsens
        self._cfg.vol_hold_sensitivity = self._wvolsens / 100.0
        self._on_save()
        self.accept()
        if restart:
            if self._ui is not None:
                self._ui['reinit_controller'] = True
                _par = self.parent()
                if _par is not None:
                    if hasattr(_par, '_refresh_controls_pill'):
                        _par._refresh_controls_pill()
                        return None
                    return None
                return None
            if self._on_restart:
                self._on_restart()
                return None
            return None
        return None

    def exec(self):
        if sys.platform == 'win32':
            try:
                hwnd = int(self.winId())
                _dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
                _on = ctypes.byref(ctypes.c_int(1))
                _dark = ctypes.byref(ctypes.c_int(0 if _active_theme() == 'light' else 1))
                _dwm(hwnd, 20, _dark, 4)
                _dwm(hwnd, 13, _on, 4)
                self._cloaked = True
                QTimer.singleShot(60, self._uncloak)
                QTimer.singleShot(160, self._uncloak)
            except Exception:
                self._cloaked = False
        return super().exec()

    def _uncloak(self):
        if not getattr(self, '_cloaked', False):
            return None
        self._cloaked = False
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()), 13, ctypes.byref(ctypes.c_int(0)), 4)
        except Exception:
            pass
        return None


    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None
class _ConfirmDialog(QDialog):
    """Themed yes/no confirm: bold title, dim body, right-aligned ghost Cancel +
    primary action. No native question icon (replaces the stock blue QMessageBox)."""

    def __init__(self, parent, title: str, body: str, confirm_text: str = 'Confirm', cancel_text: str = 'Cancel', show_dont_ask: bool = False):
        super().__init__(parent)
        self.dont_ask = False
        self.setWindowTitle('Segue')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self.setMinimumWidth(_s(360))
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(22), _s(20), _s(22), _s(18))
        v.setSpacing(_s(8))
        t = QLabel(title)
        t.setFont(_ui_font(18, QFont.Bold))
        t.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(t)
        msg = QLabel(body)
        msg.setObjectName('hint')
        msg.setFont(_ui_font(14))
        msg.setWordWrap(True)
        v.addWidget(msg)
        v.addSpacing(_s(12))
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        if show_dont_ask:
            chk = QCheckBox("Don't ask again")
            chk.setCursor(Qt.PointingHandCursor)
            chk.toggled.connect(lambda v: setattr(self, 'dont_ask', bool(v)))
            row.addWidget(chk)
        row.addStretch(1)
        if cancel_text:
            cancel = QPushButton(cancel_text)
            cancel.setObjectName('togglebtn')
            cancel.setCursor(Qt.PointingHandCursor)
            cancel.clicked.connect(self.reject)
            cancel.setDefault(True)
            row.addWidget(cancel)
        confirm = QPushButton(confirm_text)
        confirm.setObjectName('savebtn')
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(self.accept)
        if not cancel_text:
            confirm.setDefault(True)
        row.addWidget(confirm)
        v.addLayout(row)
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None


class _CustomSourceDialog(QDialog):
    """Pick a currently-playing app as the custom music source. Lists every
    process with a live audio session; clicking one selects it (self.picked)."""

    def __init__(self, parent, apps, exclude=None):
        super().__init__(parent)
        self.picked = None
        self._exclude = exclude or set()
        self.setWindowTitle('Segue')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self.setMinimumWidth(_s(380))
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(22), _s(20), _s(22), _s(18))
        v.setSpacing(_s(8))
        t = QLabel('Pick your music app')
        t.setFont(_ui_font(18, QFont.Bold))
        t.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(t)
        self._msg = QLabel('')
        self._msg.setObjectName('hint')
        self._msg.setWordWrap(True)
        v.addWidget(self._msg)
        v.addSpacing(_s(6))
        self._rows = QVBoxLayout()
        self._rows.setSpacing(_s(8))
        v.addLayout(self._rows)
        self._shown_key = None
        self._populate(apps)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        return None

    def _refresh(self):
        from fh6_spotify import source_picker
        try:
            self._populate(source_picker.list_audio_apps(exclude=self._exclude))
        except Exception:
            return None
        return None

    def _populate(self, apps):
        key = tuple((getattr(a, 'exe', '') or '').lower() for a in apps)
        if key == self._shown_key:
            return None
        self._shown_key = key
        while self._rows.count():
            it = self._rows.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if not apps:
            self._msg.setText("Nothing's playing yet. Start an app that isn't already a source and it'll show up here.")
            QTimer.singleShot(0, self.adjustSize)
            return None
        self._msg.setText('These apps are playing right now. Pick the one with your music.')
        for app in apps:
            label = (app.display or '').strip() or app.exe
            btn = QPushButton('   ' + label)
            btn.setObjectName('togglebtn')
            btn.setCursor(Qt.PointingHandCursor)
            btn.setIcon(_exe_icon(getattr(app, 'path', ''), (label[:1] or '?').upper(), 22))
            btn.setIconSize(QSize(_s(22), _s(22)))
            btn.clicked.connect(lambda _=False, a=app: self._choose(a))
            self._rows.addWidget(btn)
        QTimer.singleShot(0, self.adjustSize)
        return None

    def _choose(self, app):
        self.picked = app
        self.accept()
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None


class _DeviceCard(QToolButton):
    """First-run picker card: device icon over its name, with an accent corner tag
    ('Best' / 'Beta')."""

    def __init__(self, key: str, name: str, tag: str, icon: QIcon):
        super().__init__()
        self.key = key
        self._tag = tag
        self.setObjectName('devcard')
        self.setText(name)
        if icon is not None:
            self.setIcon(icon)
        self.setIconSize(QSize(_s(46), _s(46)))
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(_s(150), _s(108))
        return None

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._tag:
            return None
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(_ui_font(10, QFont.Bold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self._tag)
        w = tw + 2 * _s(6)
        h = fm.height() + 2 * _s(2)
        rect = QRectF(self.width() - w - _s(8), _s(8), w, h)
        bg, fg = _tag_colors(self._tag)
        _gap = float(_s(2))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_c('surface')))
        p.drawRoundedRect(rect.adjusted(-_gap, -_gap, _gap, _gap), (h + 2 * _gap) / 2, (h + 2 * _gap) / 2)
        p.setBrush(bg)
        p.drawRoundedRect(rect, h / 2, h / 2)
        p.setPen(fg)
        p.drawText(rect, Qt.AlignCenter, self._tag)
        p.end()
        return None


def _exe_friendly_name(path: str) -> str:
    """Pull a human name out of a Windows PE's version resource.  Falls back
    to '' on any failure - caller should keep its capitalized-exe-stem
    fallback for processes without a version record.

    Tries ProductName first (e.g. 'Death Stranding' on ds.exe), then
    FileDescription (e.g. 'Microsoft Edge' on msedge.exe).  Reads via
    GetFileVersionInfo / VerQueryValue from version.dll - no extra deps."""
    if not (path and os.path.exists(path)):
        return ''
    try:
        import ctypes
        from ctypes import wintypes
        version = ctypes.WinDLL('version', use_last_error=True)
        size = version.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), None)
        if not size:
            return ''
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, size, buf):
            return ''
        lp_trans = ctypes.c_void_p()
        u_len = ctypes.c_uint()
        if not version.VerQueryValueW(buf, ctypes.c_wchar_p('\\VarFileInfo\\Translation'), ctypes.byref(lp_trans), ctypes.byref(u_len)):
            return ''
        if u_len.value < 4:
            return ''
        lang_codepage = ctypes.cast(lp_trans, ctypes.POINTER(wintypes.WORD * 2))[0]
        prefix = f'\\StringFileInfo\\{lang_codepage[0]:04x}{lang_codepage[1]:04x}\\'
        for key in ('ProductName', 'FileDescription'):
            lp_val = ctypes.c_void_p()
            v_len = ctypes.c_uint()
            if not version.VerQueryValueW(buf, ctypes.c_wchar_p(prefix + key), ctypes.byref(lp_val), ctypes.byref(v_len)):
                continue
            if not v_len.value > 1:
                continue
            if not lp_val.value:
                continue
            s = ctypes.wstring_at(lp_val, v_len.value).rstrip('\x00').strip()
            if not s:
                continue
            if s.lower() not in ('n/a', 'none'):
                return s
        return ''
    except Exception:
        return ''


class _RoundedMovieLabel(QLabel):
    """QLabel that clips its QMovie / pixmap to a rounded rect. A plain QSS
    border-radius doesn't clip the movie frame - the GIF's square corners
    paint over the rounded border. This draws the current frame inside a
    rounded clip path + strokes the border on top, so the clips actually
    look rounded."""

    def __init__(self, radius: float, parent=None):
        super().__init__(parent)
        self._radius = radius
        return None

    def paintEvent(self, e):
        m = self.movie()
        pm = m.currentPixmap() if m is not None else self.pixmap()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(r, self._radius, self._radius)
        p.setClipPath(path)
        if pm is not None and not pm.isNull():
            p.drawPixmap(0, 0, pm)
        p.setClipping(False)
        pen = QPen(QColor(_c('border')))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)
        p.end()
        return None


def _dots_pixmap(size, color=None):
    """Three-dot "more" glyph for the source picker's More row, drawn at the
    same size as the source icons so the text column stays aligned."""
    color = color or _c('icon_dim')
    s = _s(size)
    pm = QPixmap(s, s)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    r = max(1.6, s * 0.085)
    for cx in (0.22, 0.5, 0.78):
        p.drawEllipse(QRectF(s * cx - r, s * 0.5 - r, 2 * r, 2 * r))
    p.end()
    return pm


def _style_expander(btn, is_open=None):
    """Quiet full-width pill for collapsible-section toggles (Controls "More
    options", Mixer "Advanced"). Neutral greys, no accent - reads clickable
    without shouting. When is_open (a callable -> bool) is given, the shipped
    chevron asset is painted just right of the centred label - down when
    collapsed, flipped up when expanded - so every dropdown/expander uses the
    same glyph at the same size instead of a unicode triangle."""
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFont(_ui_font(14))
    btn.setStyleSheet(f"QPushButton {{ background: {_c('sunk')}; border: 1px solid {_c('border')}; border-radius: {_s(7)}px; color: {_c('text_dim')}; padding: {_s(6)}px {_s(10)}px; }}QPushButton:hover {{ background: {_c('surface_hi')}; color: {_c('text')}; border-color: {_c('border_hi')}; }}")
    if is_open is not None:
        from PySide6.QtCore import QPointF, QVariantAnimation, QEasingCurve, QTimer
        down = _tinted(os.path.join(_ASSETS, 'down-chevron.png'), _c('icon_dim'), 12)
        btn._chev_angle = 180.0 if is_open() else 0.0

        def _pe(e, b=btn, d=down):
            QPushButton.paintEvent(b, e)
            if d is None or d.isNull():
                return None
            tw = b.fontMetrics().horizontalAdvance(b.text())
            cx = (b.width() + tw) / 2.0 + _s(6) + d.width() / 2.0
            cy = b.height() / 2.0
            p = QPainter(b)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.translate(cx, cy)
            p.rotate(getattr(b, '_chev_angle', 0.0))
            p.drawPixmap(QPointF(-d.width() / 2.0, -d.height() / 2.0), d)
            p.end()
            return None
        btn.paintEvent = _pe

        def _spin_chev(b=btn):
            to = 180.0 if is_open() else 0.0
            prev = getattr(b, '_chev_anim', None)
            if prev is not None:
                prev.stop()
            an = QVariantAnimation(b)
            an.setDuration(300)
            an.setStartValue(float(getattr(b, '_chev_angle', 0.0)))
            an.setEndValue(to)
            an.setEasingCurve(QEasingCurve.OutBack)
            an.valueChanged.connect(lambda v, bb=b: (setattr(bb, '_chev_angle', float(v)), bb.update()))
            an.start()
            b._chev_anim = an
            return None
        btn.clicked.connect(lambda: QTimer.singleShot(0, _spin_chev))
        return None
    return None


class _IconPopup(QFrame):
    """Frameless popup with QPushButton rows that show an icon at any size.
    QMenu hard-locks its icon size to QStyle's PM_SmallIconSize (~16 px),
    so big icons drawn from raw assets still painted tiny. QPushButton
    honours setIconSize() directly, giving us a crisp icon at any
    app-scale. Used for the source picker (Spotify / Browser) so the
    glyph grows with the rest of the UI."""

    selected = Signal(str)
    closed = Signal()

    def __init__(self, parent, items, current_key: str, icon_size: int, more_items=None, more_label: str = 'More'):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"QPushButton#popupitem {{ background: transparent; border: none; color: {_c('text')}; text-align: left; padding: {_s(8)}px {_s(12)}px; border-radius: {_s(6)}px; }}QPushButton#popupitem:hover {{ background: {_c('surface_hi')}; }}")
        self._shadow_pad = _s(18)
        self._radius = _s(8)
        self._items = list(items)
        self._more_items = list(more_items or [])
        self._more_label = more_label
        self._cur = current_key
        self._isz = icon_size
        self._v = QVBoxLayout(self)
        m = self._shadow_pad + _s(6)
        self._v.setContentsMargins(m, m, m, m)
        self._v.setSpacing(_s(2))
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(170)
        self._hover_timer.timeout.connect(lambda: self._swap(True))
        self._build(self._items, sub=False)
        return None

    def _row(self, label: str, pm, on_click, checked: bool = False):
        btn = QPushButton()
        btn.setObjectName('popupitem')
        if pm is None:
            blank = QPixmap(self._isz, self._isz)
            blank.fill(QColor(0, 0, 0, 0))
            pm = blank
        btn.setIcon(QIcon(pm))
        btn.setIconSize(QSize(self._isz, self._isz))
        btn.setText('  ' + label + ('    ✓' if checked else ''))
        btn.setFont(_ui_font(14))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(on_click)
        self._v.addWidget(btn)
        return btn

    def _build(self, items, sub: bool):
        self._more_btn = None
        while self._v.count():
            it = self._v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if sub:
            self._row('‹  Back', None, lambda _=False: self._swap(False))
        for key, label, pm in items:
            self._row(label, pm, lambda _=False, k=key: self._on_pick(k), checked=key == self._cur)
        if not sub:
            if self._more_items:
                self._more_btn = self._row(self._more_label + '  ›', _dots_pixmap(22), lambda _=False: self._swap(True))
                self._more_btn.installEventFilter(self)
                return None
            return None
        return None

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.MouseButtonPress and (not self.frameGeometry().contains(ev.globalPosition().toPoint())):
            self.close()
        if obj is getattr(self, '_more_btn', None) and obj is not None:
            if ev.type() == QEvent.Enter:
                self._hover_timer.start()
            elif ev.type() == QEvent.Leave:
                self._hover_timer.stop()
        return super().eventFilter(obj, ev)

    def _swap(self, to_sub: bool):
        self._hover_timer.stop()
        self._build(self._more_items if to_sub else self._items, sub=to_sub)
        QTimer.singleShot(0, self.adjustSize)
        return None

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pad, r = self._shadow_pad, self._radius
        panel = QRectF(self.rect()).adjusted(pad, pad, -pad, -pad)
        p.setPen(Qt.NoPen)
        for i in range(pad, 0, -1):
            a = int(14 * (1 - i / pad) ** 2)
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(panel.adjusted(-i, -i + 2, i, i + 2), r + i, r + i)
        p.setBrush(QColor(_c('panel')))
        p.setPen(QPen(QColor(_c('border')), 1))
        p.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), r, r)
        return None

    def _on_pick(self, key: str) -> None:
        self.selected.emit(key)
        self.close()
        return None

    def mousePressEvent(self, e):
        panel = self.rect().adjusted(self._shadow_pad, self._shadow_pad, -self._shadow_pad, -self._shadow_pad)
        if not panel.contains(e.position().toPoint()):
            self.close()
            return None
        super().mousePressEvent(e)
        return None

    def popup_at(self, pos: QPoint) -> None:
        self.adjustSize()
        self.move(pos - QPoint(self._shadow_pad, self._shadow_pad))
        self.show()
        return None

    def showEvent(self, e):
        super().showEvent(e)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            return None
        return None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            return None
        super().keyPressEvent(e)
        return None

    def closeEvent(self, e):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.closed.emit()
        super().closeEvent(e)
        return None


class _ResettableSlider(QSlider):
    """QSlider that resets to a default value on double-click."""

    def __init__(self, *args, default_value: int = 100, **kwargs):
        super().__init__(*args, **kwargs)
        self._default = default_value
        return None

    def mouseDoubleClickEvent(self, e):
        self.setValue(self._default)
        super().mouseDoubleClickEvent(e)
        return None


class _HoverLink(QLabel):
    """Looks and behaves like a hyperlink: brighter-white text that gains an
    underline + bold weight on hover, opens a URL on click, and can carry a
    trailing icon (e.g. the Ko-fi logo). Qt QSS can't do text-decoration on a
    QLabel, so we just re-render the rich text on enter/leave."""

    def __init__(self, text: str, url: str, icon_path: str = '', size: int = 13, parent=None):
        super().__init__(parent)
        self._t = text
        self._url = url
        self._icon = icon_path.replace('\\', '/') if icon_path and os.path.exists(icon_path) else ''
        self.setObjectName('hint')
        self.setFont(_ui_font(size))
        self.setTextFormat(Qt.RichText)
        self.setCursor(Qt.PointingHandCursor)
        self._render(False)
        return None

    def _render(self, hover: bool):
        deco = 'underline' if hover else 'none'
        sz = _s(16)
        img = f"&nbsp;<img src='{self._icon}' width='{sz}' height='{sz}' style='vertical-align:middle;'>" if self._icon else ''
        self.setText(f"<span style='color:{_c('text')}; text-decoration:{deco}; font-weight:bold;'>{self._t}</span>{img}")
        return None

    def enterEvent(self, e):
        self._render(True)
        super().enterEvent(e)
        return None

    def leaveEvent(self, e):
        self._render(False)
        super().leaveEvent(e)
        return None

    def mousePressEvent(self, e):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(self._url))
        super().mousePressEvent(e)
        return None


class _PresetsPopup(QFrame):
    """Custom presets menu: 'Save current…' row (disk glyph) + one row per
    saved preset.  Each preset row shows a trash icon on the right that
    deletes it in place (popup stays open + rebuilds), so there's no
    separate Delete submenu.  Replaces the old nested QMenu."""

    save_requested = Signal()
    load_requested = Signal(str)
    delete_requested = Signal(str)
    closed = Signal()

    def __init__(self, parent, names: list):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName('iconpopup')
        self.setStyleSheet(_build_qss(_CHECK))
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(_s(6), _s(6), _s(6), _s(6))
        self._v.setSpacing(_s(2))
        self._icon_sz = _s(16)
        self._build(names)
        return None

    def _build(self, names: list) -> None:
        while self._v.count():
            item = self._v.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        save_btn = QPushButton('  Save current…')
        save_btn.setObjectName('popupitem')
        save_btn.setIcon(QIcon(_save_pixmap(16)))
        save_btn.setIconSize(QSize(self._icon_sz, self._icon_sz))
        save_btn.setFont(_ui_font(14))
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._on_save)
        self._v.addWidget(save_btn)
        if names:
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"background: {_c('border')}; margin: {_s(4)}px {_s(4)}px;")
            self._v.addWidget(sep)
            for name in names:
                self._v.addWidget(self._preset_row(name))
            return None
        return None

    def _preset_row(self, name: str) -> QWidget:
        row = QWidget()
        row.setObjectName('presetrow')
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(_s(2))
        load_btn = QPushButton('  ' + name)
        load_btn.setObjectName('popupitem')
        load_btn.setFont(_ui_font(14))
        load_btn.setCursor(Qt.PointingHandCursor)
        load_btn.clicked.connect(lambda _=False, n=name: self._on_load(n))
        del_btn = QPushButton()
        del_btn.setObjectName('presetdel')
        del_btn.setIcon(QIcon(_trash_pixmap(16)))
        del_btn.setIconSize(QSize(self._icon_sz, self._icon_sz))
        del_btn.setFixedWidth(_s(34))
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setToolTip(f"Delete '{name}'")
        del_btn.clicked.connect(lambda _=False, n=name: self.delete_requested.emit(n))
        h.addWidget(load_btn, 1)
        h.addWidget(del_btn)
        return row

    def rebuild(self, names: list) -> None:
        """Re-render the rows after a delete (popup stays open)."""
        self._build(names)
        self.adjustSize()
        return None

    def _on_save(self) -> None:
        self.save_requested.emit()
        self.close()
        return None

    def _on_load(self, name: str) -> None:
        self.load_requested.emit(name)
        self.close()
        return None

    def popup_at(self, pos: QPoint) -> None:
        self.adjustSize()
        self.move(pos)
        self.show()
        return None

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)
        return None


class _DevicePickerDialog(QDialog):
    """One-time first-run picker: grid of input devices. The list of cards
    shown is filtered by the active game preset's supported_devices, so a
    user who picked Rocket League (PS-only in v1) doesn't see Xbox / Sim
    wheel as options."""

    _ALL_DEVS = [('playstation', 'PlayStation', 'Best'), ('xbox', 'Xbox', 'Beta'), ('keyboard', 'Keyboard', ''), ('wheel', 'Sim wheel', 'Beta')]

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Segue')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self._chosen = cfg.input_device
        from fh6_spotify import game_presets as _gp
        allowed = set(_gp.supported_devices(cfg.game_preset))
        devs = [(k, n, t) for k, n, t in self._ALL_DEVS if k in allowed]
        if not devs:
            devs = list(self._ALL_DEVS)
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(24), _s(22), _s(24), _s(22))
        v.setSpacing(_s(6))
        title = QLabel('Choose your controller')
        title.setFont(_ui_font(19, QFont.Bold))
        title.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(title)
        sub = QLabel('You can change this anytime in Controls.')
        sub.setObjectName('hint')
        sub.setFont(_ui_font(14))
        v.addWidget(sub)
        v.addSpacing(_s(12))
        grid = QGridLayout()
        grid.setSpacing(_s(10))
        for i, (key, name, tag) in enumerate(devs):
            icon = _dev_qicon(key, 46) or _load_icon(os.path.join(_ASSETS, key + '.png'), 46) or (_load_icon(os.path.join(_ASSETS, 'dualsense.png'), 46) if key == 'playstation' else None) or _dev_icon(key, 46)
            card = _DeviceCard(key, name, tag, icon)
            card.clicked.connect(lambda _=False, k=key: self._pick(k))
            grid.addWidget(card, i // 2, i % 2)
        v.addLayout(grid)
        return None

    def _pick(self, key: str):
        self._chosen = key
        self.accept()
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None

    @staticmethod
    def choose(cfg, parent=None) -> str:
        """Show the picker; return the chosen device key (or cfg's current
        if closed). When the active preset only supports one device, skip
        the dialog entirely and return that device - no reason to prompt
        the user to pick from a one-item list."""
        from fh6_spotify import game_presets as _gp
        allowed = _gp.supported_devices(cfg.game_preset)
        if len(allowed) == 1:
            return allowed[0]
        dlg = _DevicePickerDialog(cfg, parent)
        dlg.exec()
        return dlg._chosen


class _GamePresetPickerDialog(QDialog):
    """First-run picker shown BEFORE the device picker.  Lets the user
    pick the game they want Segue with.  Square icon cards for the
    curated presets (Forza, Rocket League) and a wide text row for
    'Other game' so it visually reads as the catch-all."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Segue')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self._chosen = 'forza'
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(24), _s(22), _s(24), _s(22))
        v.setSpacing(_s(6))
        title = QLabel('Which game?')
        title.setFont(_ui_font(19, QFont.Bold))
        title.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(title)
        sub = QLabel('Segue tunes itself for the game you pick.  You can switch anytime.')
        sub.setObjectName('hint')
        sub.setFont(_ui_font(14))
        sub.setWordWrap(True)
        v.addWidget(sub)
        v.addSpacing(_s(12))
        from fh6_spotify import game_presets as _gp
        card_row = QHBoxLayout()
        card_row.setSpacing(_s(10))
        for key, preset in _gp.GAME_PRESETS.items():
            if key == 'other':
                continue
            label = preset['label']
            icon_path = ''
            if key == 'forza':
                icon_path = _FORZA
            elif key == 'rocketleague':
                cand = os.path.join(_ASSETS, 'rocketleague.png')
                if os.path.exists(cand):
                    icon_path = cand
            icon = (_load_icon(icon_path, 46) if icon_path else None) or _dev_icon('xbox', 46)
            card = _DeviceCard(key, label, '', icon)
            card.clicked.connect(lambda _=False, k=key: self._pick(k))
            card_row.addWidget(card)
        v.addLayout(card_row)
        v.addSpacing(_s(8))
        other_btn = QPushButton('Other game  (pick from running processes)')
        other_btn.setObjectName('togglebtn')
        other_btn.setCursor(Qt.PointingHandCursor)
        other_btn.setMinimumHeight(_s(44))
        other_btn.setFont(_ui_font(14, QFont.Bold))
        other_btn.clicked.connect(lambda _=False: self._pick('other'))
        v.addWidget(other_btn)
        return None

    def _pick(self, key: str):
        self._chosen = key
        self.accept()
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None

    @staticmethod
    def choose(parent=None) -> str:
        dlg = _GamePresetPickerDialog(parent)
        dlg.exec()
        return dlg._chosen


class _GamePickerDialog(QDialog):
    """List currently-running processes that have an audio session - i.e.
    things you'd plausibly want Segue to listen to for speech ducking. Shown
    when the user enables "Other games" mode and clicks "Pick game"."""

    def __init__(self, parent, current: str = ''):
        super().__init__(parent)
        self.setWindowTitle('Pick app')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self.setMinimumSize(_s(360), _s(420))
        self._chosen = current
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(18), _s(16), _s(18), _s(14))
        v.setSpacing(_s(8))
        t = QLabel('Pick an app')
        t.setFont(_ui_font(17, QFont.Bold))
        t.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(t)
        sub = QLabel('Only apps currently making sound show up. Launch the app first, then pick it here.')
        sub.setObjectName('hint')
        sub.setWordWrap(True)
        v.addWidget(sub)
        v.addSpacing(_s(6))
        self._list = QListWidget()
        self._list.setFont(_ui_font(14))
        self._list.itemDoubleClicked.connect(lambda _=None: self._accept())
        v.addWidget(self._list, 1)
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        refresh = QPushButton('Refresh')
        refresh.setObjectName('togglebtn')
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self._populate)
        cancel = QPushButton('Cancel')
        cancel.setObjectName('togglebtn')
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ok = QPushButton('Use this')
        ok.setObjectName('savebtn')
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._accept)
        row.addWidget(refresh)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        self._populate()
        return None

    def _populate(self) -> None:
        """Enumerate processes that currently own an audio session - the
        same set pycaw can drive volume on. Each item shows the app's real
        Windows icon + a friendly name (no .exe). Filters out Segue + system
        processes + common non-game audio apps."""
        from PySide6.QtWidgets import QListWidgetItem, QFileIconProvider
        from PySide6.QtCore import QFileInfo
        self._list.clear()
        try:
            from pycaw.pycaw import AudioUtilities
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            sessions = []
        seen = set()
        skip = {'', 'afterfx.exe', 'amazon music.exe', 'applemusic.exe', 'audacity.exe', 'audiodg.exe', 'battle.net.exe', 'brave.exe', 'broadcastdvr.exe', 'chrome.exe', 'discord.exe', 'ealauncher.exe', 'epicgameslauncher.exe', 'explorer.exe', 'firefox.exe', 'gamebar.exe', 'gamebarftserver.exe', 'groove.exe', 'librewolf.exe', 'medal.exe', 'medalencoder.exe', 'microsoft.media.player.exe', 'msedge.exe', 'nvcontainer.exe', 'nvidia share.exe', 'obs.exe', 'obs32.exe', 'obs64.exe', 'opera.exe', 'premierepro.exe', 'resolve.exe', 'rockstargameslauncher.exe', 'segue.exe', 'shadowplay.exe', 'spotify.exe', 'steam.exe', 'streamlabs obs.exe', 'system', 'tidal.exe', 'uplay.exe', 'vivaldi.exe', 'vlc.exe', 'windowsterminal.exe', 'wispr flow.exe', 'wmplayer.exe', 'xboxgamebar.exe'}
        skip_fragments = ('overlay', 'renderer', 'helper', 'service', 'background', 'encoder', 'recorder', 'launcher', 'crashreport', 'updater', 'installer')
        provider = QFileIconProvider()
        for s in sessions:
            try:
                if s.Process is None:
                    continue
                proc = s.Process
                name = (proc.name() or '').lower()
                if name in skip or name in seen:
                    continue
                stem = name.rsplit('.', 1)[0]
                if any(frag in stem for frag in skip_fragments):
                    continue
                seen.add(name)
                try:
                    exe = proc.exe()
                except Exception:
                    exe = ''
                pretty = _exe_friendly_name(exe) if exe else ''
                if not pretty:
                    stem_name = name.rsplit('.', 1)[0]
                    pretty = stem_name[:1].upper() + stem_name[1:] if stem_name else name
                item = QListWidgetItem(pretty)
                try:
                    if exe:
                        icon = provider.icon(QFileInfo(exe))
                        if not icon.isNull():
                            item.setIcon(icon)
                except Exception:
                    pass
                item.setData(Qt.UserRole, name)
                self._list.addItem(item)
                if name == self._chosen:
                    self._list.setCurrentRow(self._list.count() - 1)
            except Exception:
                continue
        self._list.setIconSize(QSize(_s(22), _s(22)))
        if self._list.count() == 0:
            placeholder = QListWidgetItem('no game detected. Launch a game first.')
            placeholder.setData(Qt.UserRole, '')
            self._list.addItem(placeholder)
            return None
        return None

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.UserRole)
        if not name:
            return None
        self._chosen = name
        self.accept()
        return None

    @staticmethod
    def pick(parent, current: str = '') -> str:
        dlg = _GamePickerDialog(parent, current)
        if dlg.exec() == QDialog.Accepted:
            return dlg._chosen
        return current


class _OverlayPositionPicker(QWidget):
    """Mini schematic monitor with the now-playing overlay drawn at the
    selected anchor. Click an area = snap to one of 6 presets and fire
    `changed(key)`. Click + drag the pill anywhere = custom placement and
    fire `custom_changed(x_pct, y_pct)`. Right-click resets to preset."""

    from PySide6.QtCore import Signal as _Signal
    changed = _Signal(str)
    custom_changed = _Signal(float, float)
    _SLOTS = [('top_left', 0, 0), ('top_center', 0, 1), ('top_right', 0, 2), ('middle_left', 1, 0), ('middle_center', 1, 1), ('middle_right', 1, 2), ('bottom_left', 2, 0), ('bottom_center', 2, 1), ('bottom_right', 2, 2)]
    _DRAG_THRESHOLD = 4

    def __init__(self, current: str = 'middle_left', custom_x: float = -1.0, custom_y: float = -1.0):
        super().__init__()
        self._current = current
        self._cx = custom_x
        self._cy = custom_y
        self._hover = None
        self._press_pt = None
        self._is_dragging = False
        self._free_mode = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(_s(140), _s(90))
        self.setMouseTracking(True)
        self._refresh_tooltip()
        return None

    def set_free_mode(self, on: bool) -> None:
        self._free_mode = bool(on)
        self._refresh_tooltip()
        return None

    def _refresh_tooltip(self) -> None:
        if self._free_mode:
            self.setToolTip('Drag the overlay anywhere. Double-click to snap back to the nearest preset (right-click also resets).')
            return None
        self.setToolTip('Click a spot to snap to a preset, or drag the pill anywhere for free placement. Double-click / right-click resets.')
        return None

    def _screen_rect(self) -> QRect:
        m = _s(6)
        return self.rect().adjusted(m, m, -m, -m)

    def _overlay_rect_for(self, key: str) -> QRect:
        """Where the orange overlay-pill sits inside the screen rect for a
        given anchor key. Pill is sized to look like the real overlay."""
        scr = self._screen_rect()
        pad = _s(4)
        pw, ph = _s(34), _s(14)
        if key.endswith('_right'):
            x = scr.right() - pw - pad
        elif key.endswith('_center'):
            x = scr.center().x() - pw // 2
        else:
            x = scr.left() + pad
        if key.startswith('top_'):
            y = scr.top() + pad
        elif key.startswith('bottom_'):
            y = scr.bottom() - ph - pad
        else:
            y = scr.center().y() - ph // 2
        return QRect(x, y, pw, ph)

    def _custom_overlay_rect(self) -> QRect:
        """Pill position derived from the custom 0..1 percentages."""
        scr = self._screen_rect()
        pw, ph = _s(34), _s(14)
        x = scr.left() + int(self._cx * (scr.width() - pw))
        y = scr.top() + int(self._cy * (scr.height() - ph))
        return QRect(x, y, pw, ph)

    def _has_custom(self) -> bool:
        return 0.0 <= self._cx <= 1.0 and 0.0 <= self._cy <= 1.0

    def _active_rect(self) -> QRect:
        if self._has_custom():
            return self._custom_overlay_rect()
        return self._overlay_rect_for(self._current)

    def _set_custom_from_point(self, pt) -> None:
        """Convert a cursor pos to (cx, cy) percent + update + emit."""
        scr = self._screen_rect()
        pw, ph = _s(34), _s(14)
        x = pt.x() - pw // 2
        y = pt.y() - ph // 2
        span_x = max(1, scr.width() - pw)
        span_y = max(1, scr.height() - ph)
        cx = (x - scr.left()) / span_x
        cy = (y - scr.top()) / span_y
        cx = max(0.0, min(1.0, cx))
        cy = max(0.0, min(1.0, cy))
        self._cx, self._cy = cx, cy
        self.update()
        self.custom_changed.emit(cx, cy)
        return None

    def _nearest_anchor(self, pt) -> str:
        scr = self._screen_rect()
        third = scr.height() // 3
        rel_y = pt.y() - scr.top()
        if rel_y < third:
            row = 'top'
        elif rel_y < third * 2:
            row = 'middle'
        else:
            row = 'bottom'
        w3 = max(1, scr.width() // 3)
        rel_x = pt.x() - scr.left()
        if rel_x < w3:
            col = 'left'
        elif rel_x < w3 * 2:
            col = 'center'
        else:
            col = 'right'
        return f'{row}_{col}'

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and self._press_pt is not None:
            if not self._is_dragging:
                if (e.pos() - self._press_pt).manhattanLength() >= self._DRAG_THRESHOLD:
                    self._is_dragging = True
            if self._is_dragging:
                self._set_custom_from_point(e.pos())
                return None
        key = self._nearest_anchor(e.pos())
        if key != self._hover:
            self._hover = key
            self.update()
            return None
        return None

    def leaveEvent(self, e):
        if self._hover is not None:
            self._hover = None
            self.update()
            return None
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            if self._has_custom():
                self._cx = self._cy = -1.0
                self.update()
                self.custom_changed.emit(-1.0, -1.0)
            return None
        if e.button() == Qt.LeftButton:
            self._press_pt = e.pos()
            self._is_dragging = False
            if self._free_mode:
                self._is_dragging = True
                self._set_custom_from_point(e.pos())
                return None
            return None
        return None

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return None
        if self._is_dragging:
            self._is_dragging = False
            self._press_pt = None
            return None
        self._press_pt = None
        if self._free_mode:
            return None
        key = self._nearest_anchor(e.pos())
        if not key:
            return None
        if self._has_custom():
            self._cx = self._cy = -1.0
            self.custom_changed.emit(-1.0, -1.0)
        if key != self._current:
            self._current = key
            self.changed.emit(key)
        self.update()
        return None

    def mouseDoubleClickEvent(self, e):
        """Snap the pill to the nearest preset anchor and clear any free-mode
        custom placement. Works in both modes - it's the universal "reset"
        gesture on the picker (matches the slider's double-click-to-default)."""
        if e.button() != Qt.LeftButton:
            return None
        self._is_dragging = False
        self._press_pt = None
        if self._has_custom():
            self._cx = self._cy = -1.0
            self.custom_changed.emit(-1.0, -1.0)
        key = self._nearest_anchor(e.pos()) or self._current
        if key and key != self._current:
            self._current = key
            self.changed.emit(key)
        self.update()
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scr = self._screen_rect()
        bezel = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(_c('border_hi')), 1.4))
        p.setBrush(QColor('#bcbcb9' if _active_theme() == 'light' else '#1a1a18'))
        p.drawRoundedRect(bezel, _s(6), _s(6))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_c('panel')))
        p.drawRoundedRect(scr, _s(3), _s(3))
        if self._hover and not self._is_dragging:
            from PySide6.QtCore import QRectF
            for key, _row, _col in self._SLOTS:
                gr = self._overlay_rect_for(key)
                bright = key == self._hover
                _gc = QColor(_c('icon'))
                _gc.setAlpha(120 if bright else 45)
                p.setPen(QPen(_gc, 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(gr).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        active = self._active_rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_ACCENT))
        p.drawRoundedRect(active, 3, 3)
        p.end()
        return None


def _make_about_page():
    """Shared closing slide for the first-run gates: a solo-dev note with the
    Discord as the primary call-to-action and a soft Ko-fi link below. Drops
    into either gate's QStackedWidget as the final page."""
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl
    w = QWidget()
    pv = QVBoxLayout(w)
    pv.setContentsMargins(_s(8), _s(8), _s(8), _s(8))
    pv.setSpacing(_s(16))
    pv.addStretch(1)
    t = QLabel('Made by one person')
    t.setFont(_ui_font(20, QFont.Bold))
    t.setStyleSheet(f"color:{_c('text')};")
    t.setAlignment(Qt.AlignCenter)
    pv.addWidget(t)
    body = QLabel("I'm a solo developer and I built Segue myself.<br><br>It's a work in progress, so bugs can happen.<br><br>Got a bug or an idea? Join the Discord and I'll read it.")
    body.setObjectName('hint')
    body.setFont(_ui_font(16))
    body.setWordWrap(True)
    body.setAlignment(Qt.AlignCenter)
    body.setTextFormat(Qt.RichText)
    pv.addWidget(body)
    pv.addStretch(1)
    btn = QPushButton('Join the Discord')
    btn.setObjectName('savebtn')
    btn.setCursor(Qt.PointingHandCursor)
    dicon = _load_icon(os.path.join(_ASSETS, 'Discord_logo_blue.png'), 20)
    if dicon:
        btn.setIcon(dicon)
        btn.setIconSize(QSize(_s(20), _s(20)))
    btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(_DISCORD_URL)))
    brow = QHBoxLayout()
    brow.addStretch(1)
    brow.addWidget(btn)
    brow.addStretch(1)
    pv.addLayout(brow)
    kofi = _HoverLink('or support development on Ko-fi', _SUPPORT_URL, icon_path=os.path.join(_ASSETS, 'kofi_logo.png'), size=13)
    pv.addWidget(kofi, 0, Qt.AlignHCenter)
    pv.addStretch(1)
    return w


class _WelcomeDialog(QDialog):
    """First-run greeting before the spotlight tour. Start / Skip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Segue')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(14))
        self.setMinimumWidth(_s(380))
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(22), _s(20), _s(22), _s(18))
        v.setSpacing(_s(8))
        t = QLabel('Welcome to Segue!')
        t.setFont(_ui_font(20, QFont.Bold))
        t.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(t)
        body = QLabel(f"Quick guided tour of what each part of the window does. Takes ~20 seconds. <span style='color:{_ACCENT}; font-weight:bold;'>(recommended)</span>")
        body.setObjectName('hint')
        body.setFont(_ui_font(14))
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        v.addWidget(body)
        v.addSpacing(_s(12))
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        row.addStretch(1)
        skip = QPushButton('Skip')
        skip.setObjectName('togglebtn')
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(self.reject)
        start = QPushButton('Start tour')
        start.setObjectName('savebtn')
        start.setCursor(Qt.PointingHandCursor)
        start.clicked.connect(self.accept)
        start.setDefault(True)
        row.addWidget(skip)
        row.addWidget(start)
        v.addLayout(row)
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None


class _TourCallout(QFrame):
    """Floating panel anchored beside the spotlighted widget. Title + body +
    counter + Next / Skip."""

    def __init__(self, parent, on_next, on_skip):
        super().__init__(parent)
        self.setObjectName('tourcallout')
        self.setStyleSheet(f"QFrame#tourcallout {{ background: {_c('panel')}; border: 2px solid {_ACCENT}; border-radius: {_s(10)}px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(_s(14), _s(12), _s(14), _s(12))
        v.setSpacing(_s(6))
        self._title = QLabel('')
        self._title.setFont(_ui_font(15, QFont.Bold))
        self._title.setStyleSheet(f"color: {_c('text')};")
        v.addWidget(self._title)
        self._body = QLabel('')
        self._body.setObjectName('hint')
        self._body.setFont(_ui_font(13))
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.RichText)
        v.addWidget(self._body)
        v.addSpacing(_s(4))
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        self._counter = QLabel('')
        self._counter.setObjectName('hint')
        self._counter.setFont(_ui_font(12))
        row.addWidget(self._counter)
        row.addStretch(1)
        self._skip_btn = QPushButton('Skip')
        self._skip_btn.setObjectName('togglebtn')
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(on_skip)
        self._next = QPushButton('Next')
        self._next.setObjectName('savebtn')
        self._next.setCursor(Qt.PointingHandCursor)
        self._next.clicked.connect(on_next)
        self._skip_btn.setFocusPolicy(Qt.NoFocus)
        self._next.setFocusPolicy(Qt.NoFocus)
        row.addWidget(self._skip_btn)
        row.addWidget(self._next)
        v.addLayout(row)
        self.setMinimumWidth(_s(280))
        return None

    def set_step(self, title: str, body: str, idx: int, total: int, final_label: str = 'Done', secondary_label: str = 'Skip'):
        self._title.setText(title)
        self._body.setText(body)
        self._counter.setText(f'{idx + 1} of {total}')
        last = idx + 1 == total
        self._next.setText(final_label if last else 'Next')
        self._skip_btn.setText(secondary_label if last else 'Skip')
        return None


class _StreamOverlayEditor(QDialog):
    """Launcher for the OBS stream overlay: enable it, copy the OBS source URL, and
    open the live VISUAL editor (a browser page that shows the real overlay and lets
    you drag-resize + restyle it, saving live). Toggling writes the app's own config,
    so there's no save race."""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QCheckBox, QLineEdit, QPushButton, QLabel
        from fh6_spotify.overlay_server import DEFAULT_PORT
        self._cfg = cfg
        port = getattr(cfg, 'stream_overlay_port', DEFAULT_PORT)
        self._base = f'http://127.0.0.1:{port}'
        self.setWindowTitle('Stream overlay')
        self.setMinimumWidth(_s(400))
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        v = QVBoxLayout(self)
        v.setSpacing(_s(11))
        v.setContentsMargins(_s(18), _s(16), _s(18), _s(16))
        self._en = QCheckBox('Enable stream overlay (OBS browser source)')
        self._en.setChecked(bool(getattr(cfg, 'stream_overlay', False)))
        self._en.setCursor(Qt.PointingHandCursor)
        self._en.toggled.connect(self._on_enable)
        v.addWidget(self._en)
        v.addWidget(QLabel('OBS Browser Source URL:'))
        row = QHBoxLayout()
        le = QLineEdit(self._base + '/')
        le.setReadOnly(True)
        cp = QPushButton('Copy')
        cp.setCursor(Qt.PointingHandCursor)
        cp.clicked.connect(lambda: self._copy(self._base + '/'))
        row.addWidget(le, 1)
        row.addWidget(cp)
        v.addLayout(row)
        hint = QLabel("In OBS: Sources → + → Browser Source → paste this. It's transparent; size + position it in OBS.")
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        v.addWidget(hint)
        self._ed = QPushButton('Open visual editor')
        self._ed.setObjectName('savebtn')
        self._ed.setCursor(Qt.PointingHandCursor)
        self._ed.clicked.connect(self._open_editor)
        v.addWidget(self._ed)
        ehint = QLabel('Opens in your browser: drag the handle to resize, set background, colors, cover shape, show/hide. Saves + goes live instantly.')
        ehint.setObjectName('hint')
        ehint.setWordWrap(True)
        v.addWidget(ehint)
        self._sync()
        return None

    def _copy(self, text):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        return None

    def _open_editor(self):
        import webbrowser
        webbrowser.open('https://getsegue.app/editor')
        return None

    def _srv(self):
        try:
            from PySide6.QtWidgets import QApplication
            return QApplication.instance()._segue.get('overlay_srv')
        except Exception:
            return None

    def _on_enable(self, on):
        self._cfg.stream_overlay = bool(on)
        try:
            from fh6_spotify.config import default_config_path
            self._cfg.save(default_config_path())
        except Exception:
            pass
        srv = self._srv()
        if srv is not None:
            srv.start() if on else srv.stop()
        self._sync()
        return None

    def _sync(self):
        self._ed.setEnabled(self._en.isChecked())
        return None


class _ForzaSetupGate(QDialog):
    """First-run Forza must-do gate as a 3-step wizard: one step per page so
    it's obvious it's just three easy steps. Step 1 (telemetry) is the
    critical one. Shown before the app so users don't miss telemetry setup -
    the #1 'it doesn't work' cause."""

    def __init__(self, device: str = 'playstation', parent=None):
        super().__init__(parent)
        self.setWindowTitle('Segue - Forza setup')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setModal(True)
        self.setFixedWidth(_s(600))
        self.setMinimumHeight(_s(540))
        is_ps = device in ('playstation', 'dualsense', 'dualshock')
        uses_dpad = device != 'keyboard'
        self._movies = []
        root = QVBoxLayout(self)
        root.setContentsMargins(_s(22), _s(18), _s(22), _s(16))
        root.setSpacing(_s(10))
        self._warn = QLabel('⚠  IMPORTANT')
        self._warn.setAlignment(Qt.AlignCenter)
        self._warn.setFont(_ui_font(26, QFont.Bold))
        self._warn.setStyleSheet(f'color: {_ACCENT};')
        root.addWidget(self._warn)
        self._warn2 = QLabel("Do these or Segue won't work.")
        self._warn2.setAlignment(Qt.AlignCenter)
        self._warn2.setWordWrap(True)
        self._warn2.setFont(_ui_font(13, QFont.Bold))
        self._warn2.setStyleSheet(f"color: {_c('text')};")
        root.addWidget(self._warn2)
        self._step_lbl = QLabel('Step 1 of 3')
        self._step_lbl.setObjectName('hint')
        self._step_lbl.setAlignment(Qt.AlignCenter)
        self._step_lbl.setFont(_ui_font(14, QFont.Bold))
        root.addWidget(self._step_lbl)

        def page(title, blocks):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            _smooth_scroll(sc)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            w = QWidget()
            pv = QVBoxLayout(w)
            pv.setContentsMargins(_s(6), _s(6), _s(6), _s(6))
            pv.setSpacing(_s(14))
            t = QLabel(title)
            t.setFont(_ui_font(20, QFont.Bold))
            t.setStyleSheet(f"color: {_c('text')};")
            t.setWordWrap(True)
            pv.addWidget(t)
            for kind, val in blocks:
                if kind == 'text':
                    p = QLabel(val)
                    p.setObjectName('hint')
                    p.setFont(_ui_font(16))
                    p.setWordWrap(True)
                    p.setTextFormat(Qt.RichText)
                    p.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    pv.addWidget(p)
                elif kind == 'img':
                    path = os.path.join(_ASSETS, val)
                    if not os.path.exists(path):
                        continue
                    pm = QPixmap(path)
                    if pm.isNull():
                        continue
                    pm = pm.scaledToWidth(_s(510), Qt.SmoothTransformation)
                    if pm.height() > _s(340):
                        pm = pm.scaledToHeight(_s(340), Qt.SmoothTransformation)
                    il = QLabel()
                    il.setPixmap(pm)
                    il.setStyleSheet(f"border: 1px solid {_c('border')}; border-radius: {_s(4)}px;")
                    il.setFixedSize(pm.size())
                    pv.addWidget(il, 0, Qt.AlignHCenter)
            pv.addStretch(1)
            sc.setWidget(w)
            return sc

        self._stack = QStackedWidget()
        self._stack.addWidget(page('Turn ON Data Out', [
            ('text', "Without this, Segue can't follow your driving."),
            ('text', 'Forza → <b>Settings</b> → <b>HUD &amp; Gameplay</b> → <b>Telemetry</b>, then set:'),
            ('text', 'Data Out: <b>On</b><br>IP Address: <b>127.0.0.1</b><br>Port: <b>5300</b>'),
            ('img', 'setup_telemetry.png'),
        ]))
        if uses_dpad and is_ps:
            self._stack.addWidget(page('Free the D-pad for skipping', [
                ('text', 'Segue skips with <b>D-pad Left / Right</b>, so free them up:'),
                ('text', '<b>1.</b> Unbind <b>Radio Prev / Next</b> AND <b>Telemetry Prev / Next</b>.'),
                ('img', 'setup_controls.png'),
                ('text', '<b>2.</b> Move <b>Forza LINK</b> off the D-pad onto the <b>View button</b> (left of the touchpad).'),
                ('img', 'setup_link2.png'),
            ]))
        elif uses_dpad:
            self._stack.addWidget(page('Free the D-pad for skipping', [
                ('text', 'Segue skips with <b>D-pad Left / Right</b>, so free them up:'),
                ('text', 'Unbind <b>Radio Prev / Next</b> AND <b>Telemetry Prev / Next</b>.'),
                ('img', 'setup_controls.png'),
            ]))
        self._stack.addWidget(page("Turn the game's music to 0", [
            ('text', 'Forza → pause → Audio → <b>Music: 0</b>.'),
            ('text', "So Forza's own music doesn't play over Spotify."),
            ('img', 'setup_music.png'),
            ('text', 'Full guide w/ pictures: <b>menu → Help → Setup</b>.'),
        ]))
        self._setup_steps = self._stack.count()
        self._controls_idx = self._stack.count()
        self._stack.addWidget(self._controls_page(device))
        self._about_idx = self._stack.count()
        self._stack.addWidget(_make_about_page())
        root.addWidget(self._stack, 1)
        btns = QHBoxLayout()
        self._skip_btn = QPushButton("I'll set it up later")
        self._skip_btn.setObjectName('togglebtn')
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(self.reject)
        self._back_btn = QPushButton('Back')
        self._back_btn.setObjectName('togglebtn')
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.clicked.connect(lambda: self._go(-1))
        self._next_btn = QPushButton('Next  →')
        self._next_btn.setObjectName('savebtn')
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.clicked.connect(lambda: self._go(1))
        btns.addWidget(self._skip_btn)
        btns.addStretch(1)
        btns.addWidget(self._back_btn)
        btns.addWidget(self._next_btn)
        root.addLayout(btns)
        self._sync_nav()
        return None

    def _go(self, delta: int) -> None:
        i = self._stack.currentIndex() + delta
        if i < 0:
            return None
        if i >= self._stack.count():
            self.accept()
            return None
        self._stack.setCurrentIndex(i)
        self._sync_nav()
        return None

    def _sync_nav(self) -> None:
        i = self._stack.currentIndex()
        n = self._stack.count()
        ns = getattr(self, '_setup_steps', n)
        if i < ns:
            self._warn.setText('⚠  IMPORTANT')
            self._warn2.setText(f"Do these or Segue won't work. Just {ns} quick steps.")
            self._step_lbl.setText(f'Step {i + 1} of {ns}')
        elif i == getattr(self, '_controls_idx', -1):
            self._warn.setText('Your controls')
            self._warn2.setText('How to run Spotify while you drive.')
            self._step_lbl.setText('')
        else:
            self._warn.setText('Before you go')
            self._warn2.setText('A quick note from me.')
            self._step_lbl.setText('')
        self._back_btn.setEnabled(i > 0)
        self._next_btn.setText('Got it' if i == n - 1 else 'Next  →')
        return None

    def _ctrl_keycaps(self, keys):
        cont = QWidget()
        h = QHBoxLayout(cont)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(_s(8))
        h.addStretch(1)
        for k in keys:
            lbl = QLabel(k)
            lbl.setFont(_ui_font(16, QFont.Bold))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color:{_c('text')}; background:{_c('deep')}; border:2px solid {_c('border_hi')}; border-radius:{_s(8)}px; padding:{_s(8)}px {_s(14)}px;")
            h.addWidget(lbl)
        h.addStretch(1)
        return cont

    def _ctrl_gif(self, clip, w=150):
        from PySide6.QtGui import QImageReader
        p = os.path.join(_ASSETS, clip)
        if not os.path.exists(p):
            return QLabel()
        src = QImageReader(p).size()
        tw = _s(w)
        th = int(tw * src.height() / src.width()) if src.isValid() and src.width() else _s(90)
        lbl = _RoundedMovieLabel(_s(6))
        mv = QMovie(p)
        mv.setScaledSize(QSize(tw, th))
        lbl.setMovie(mv)
        mv.start()
        self._movies.append(mv)
        lbl.setFixedSize(QSize(tw, th))
        return lbl

    def _ctrl_row(self, visual, action, how):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(_s(14))
        cell = QWidget()
        cell.setFixedWidth(_s(170))
        cv = QVBoxLayout(cell)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addStretch(1)
        cv.addWidget(visual, 0, Qt.AlignCenter)
        cv.addStretch(1)
        h.addWidget(cell)
        txt = QWidget()
        tv = QVBoxLayout(txt)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(_s(2))
        a = QLabel(action)
        a.setFont(_ui_font(16, QFont.Bold))
        a.setStyleSheet(f"color:{_c('text')};")
        c = QLabel(how)
        c.setObjectName('hint')
        c.setFont(_ui_font(14))
        c.setWordWrap(True)
        tv.addStretch(1)
        tv.addWidget(a)
        tv.addWidget(c)
        tv.addStretch(1)
        h.addWidget(txt, 1)
        return row

    def _controls_page(self, device):
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        _smooth_scroll(sc)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        w = QWidget()
        pv = QVBoxLayout(w)
        pv.setContentsMargins(_s(6), _s(8), _s(6), _s(8))
        pv.setSpacing(_s(14))
        intro = QLabel("Once you're set up, here's how to drive Spotify:")
        intro.setObjectName('hint')
        intro.setFont(_ui_font(15))
        intro.setWordWrap(True)
        pv.addWidget(intro)
        if device in ('playstation', 'dualsense', 'dualshock'):
            pv.addWidget(self._ctrl_row(self._ctrl_gif('clips/touchpad_swipe_up.gif'), 'Volume', 'Swipe up / down on the touchpad'))
            pv.addWidget(self._ctrl_row(self._ctrl_gif('clips/dpad_press_right_left.gif'), 'Skip track', 'D-pad Left / Right'))
            pv.addWidget(self._ctrl_row(self._ctrl_gif('clips/touchpad_tap.gif'), 'Pause / play', 'Tap the touchpad'))
        elif device == 'keyboard':
            pv.addWidget(self._ctrl_row(self._ctrl_keycaps(['=', '-']), 'Volume', '= louder, - quieter'))
            pv.addWidget(self._ctrl_row(self._ctrl_keycaps([']', '[']), 'Skip track', '] next, [ previous'))
            pv.addWidget(self._ctrl_row(self._ctrl_keycaps(['\\']), 'Pause / play', 'Backslash key'))
        elif device == 'xbox':
            pv.addWidget(self._ctrl_row(self._ctrl_gif('clips/dpad_press_right_left.gif'), 'Skip track', 'D-pad Left / Right'))
            pv.addWidget(self._ctrl_row(QLabel(), 'Volume / Pause', 'Pick free buttons in Controls after setup'))
        else:
            pv.addWidget(self._ctrl_row(QLabel(), 'All controls', 'Bind them in Controls after setup'))
        foot = QLabel('Everything here is rebindable in Controls.')
        foot.setObjectName('hint')
        foot.setFont(_ui_font(13))
        foot.setWordWrap(True)
        pv.addWidget(foot)
        pv.addStretch(1)
        sc.setWidget(w)
        return sc

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None


class _GeneralIntroGate(QDialog):
    """First-run controls intro for plug-and-play (general-mode) games: Rocket
    League and any 'Other game'. No setup steps - it showcases the controls for
    the picked device, then a closing solo-dev slide.

    game_label appears in the title / intro line ("" -> generic 'Other game'
    wording). PlayStation gets the touchpad gesture GIFs, keyboard gets
    key-caps, and Xbox / sim wheel get a 'bind it in Controls' slide (general
    mode ships no default buttons for them)."""

    def __init__(self, device: str = 'playstation', game_label: str = 'Rocket League', parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Segue - {game_label}' if game_label else 'Segue - Controls')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setModal(True)
        self.setFixedWidth(_s(540))
        self.setMinimumHeight(_s(500))
        self._movies = []
        is_kb = device == 'keyboard'
        is_touch = device in ('playstation', 'dualsense', 'dualshock')
        music_owner = game_label if game_label else 'the game'
        root = QVBoxLayout(self)
        root.setContentsMargins(_s(22), _s(18), _s(22), _s(16))
        root.setSpacing(_s(10))
        head_row = QHBoxLayout()
        head_row.setSpacing(_s(8))
        head_row.addStretch(1)
        if is_kb:
            _hic = _dev_qicon('keyboard', 24) or _dev_icon('keyboard', 24)
            head_txt = 'Your keyboard is the remote'
        elif is_touch:
            _hic = _dev_qicon('playstation', 24) or _load_icon(os.path.join(_ASSETS, 'dualsense.png'), 24) or _dev_icon('playstation', 24)
            head_txt = 'Your touchpad is the remote'
        else:
            _hic = _dev_qicon(device, 24) or _dev_icon(device, 24)
            head_txt = 'Set up your controls'
        _hic_lbl = QLabel()
        _hic_lbl.setPixmap(_hic.pixmap(QSize(_s(24), _s(24))))
        head_row.addWidget(_hic_lbl, 0, Qt.AlignVCenter)
        head = QLabel(head_txt)
        head.setFont(_ui_font(20, QFont.Bold))
        head.setStyleSheet(f"color: {_c('text')};")
        head_row.addWidget(head, 0, Qt.AlignVCenter)
        head_row.addStretch(1)
        root.addLayout(head_row)
        lead = f'{game_label} is plug & play, no setup.' if game_label else 'Plug and play, no setup.'
        sub = QLabel(lead + " Here's how to run Spotify while you play:")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setObjectName('hint')
        sub.setFont(_ui_font(14))
        root.addWidget(sub)
        self._step_lbl = QLabel('Step 1 of 3')
        self._step_lbl.setObjectName('hint')
        self._step_lbl.setAlignment(Qt.AlignCenter)
        self._step_lbl.setFont(_ui_font(14, QFont.Bold))
        root.addWidget(self._step_lbl)
        from PySide6.QtGui import QImageReader

        def keycap(text):
            k = QLabel(text)
            k.setFont(_ui_font(15, QFont.Bold))
            k.setAlignment(Qt.AlignCenter)
            k.setStyleSheet(f"color:{_c('text')}; background:{_c('deep')}; border:2px solid {_c('border_hi')}; border-radius:{_s(8)}px; padding:{_s(10)}px {_s(18)}px;")
            return k

        def page(title, clip, lines, keys=None, note=None):
            w = QWidget()
            pv = QVBoxLayout(w)
            pv.setContentsMargins(_s(2), _s(4), _s(8), _s(4))
            pv.setSpacing(_s(10))
            pv.addStretch(1)
            t = QLabel(title)
            t.setFont(_ui_font(18, QFont.Bold))
            t.setStyleSheet(f"color: {_c('text')};")
            t.setAlignment(Qt.AlignCenter)
            pv.addWidget(t)
            if keys:
                rowl = QHBoxLayout()
                rowl.setSpacing(_s(12))
                rowl.addStretch(1)
                for kt in keys:
                    rowl.addWidget(keycap(kt), 0, Qt.AlignVCenter)
                rowl.addStretch(1)
                pv.addLayout(rowl)
            elif clip:
                p_clip = os.path.join(_ASSETS, clip)
                if os.path.exists(p_clip):
                    src = QImageReader(p_clip).size()
                    tw = _s(300)
                    th = int(tw * src.height() / src.width()) if src.isValid() and src.width() else _s(170)
                    lbl = _RoundedMovieLabel(_s(6))
                    mv = QMovie(p_clip)
                    mv.setScaledSize(QSize(tw, th))
                    lbl.setMovie(mv)
                    mv.start()
                    self._movies.append(mv)
                    lbl.setFixedSize(QSize(tw, th))
                    rowl = QHBoxLayout()
                    rowl.addStretch(1)
                    rowl.addWidget(lbl)
                    rowl.addStretch(1)
                    pv.addLayout(rowl)
            cap = QLabel('\n'.join(lines))
            cap.setObjectName('hint')
            cap.setFont(_ui_font(15))
            cap.setWordWrap(True)
            cap.setAlignment(Qt.AlignCenter)
            pv.addWidget(cap)
            if note:
                nt, nb = note
                pv.addSpacing(_s(4))
                ntl = QLabel(nt)
                ntl.setFont(_ui_font(14, QFont.Bold))
                ntl.setStyleSheet(f'color:{_ACCENT};')
                ntl.setAlignment(Qt.AlignCenter)
                pv.addWidget(ntl)
                nbl = QLabel(nb)
                nbl.setObjectName('hint')
                nbl.setFont(_ui_font(13))
                nbl.setWordWrap(True)
                nbl.setAlignment(Qt.AlignCenter)
                pv.addWidget(nbl)
            pv.addStretch(1)
            return w

        rebind_note = ('Rebindable', f"Change any of these in Controls. Tip: set {music_owner}'s music to 0 so it doesn't clash with Spotify.")
        self._stack = QStackedWidget()
        if is_kb:
            self._stack.addWidget(page('Volume', '', ['Page Up = louder, Page Down = quieter.'], keys=['Page Up', 'Page Down']))
            self._stack.addWidget(page('Skip / previous track', '', ['End = next track, Home = previous.'], keys=['End', 'Home']))
            self._stack.addWidget(page('Pause / play', '', ['Insert = pause / play.'], keys=['Insert'], note=rebind_note))
        elif is_touch:
            self._stack.addWidget(page('Volume', 'clips/touchpad_swipe_up.gif', ['Swipe up / down on the touchpad.']))
            self._stack.addWidget(page('Skip / previous track', 'clips/touchpad_swipe_right.gif', ['Swipe right = next track, left = previous.']))
            self._stack.addWidget(page('Pause / play', 'clips/touchpad_tap.gif', ['Tap the touchpad (or set it to Press in Controls).'], note=rebind_note))
        else:
            self._stack.addWidget(page('Set your buttons', '', ['Segue has no preset buttons for your controller here.', '', "Open Controls to bind Skip, Volume and Pause to buttons you don't use in-game.", '', f"Tip: set {music_owner}'s music to 0 so it doesn't clash with Spotify."]))
        self._stack.addWidget(_make_about_page())
        root.addWidget(self._stack, 1)
        btns = QHBoxLayout()
        self._skip_btn = QPushButton('Skip')
        self._skip_btn.setObjectName('togglebtn')
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(self.accept)
        self._back_btn = QPushButton('Back')
        self._back_btn.setObjectName('togglebtn')
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.clicked.connect(lambda: self._go(-1))
        self._next_btn = QPushButton('Next  →')
        self._next_btn.setObjectName('savebtn')
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.clicked.connect(lambda: self._go(1))
        btns.addWidget(self._skip_btn)
        btns.addStretch(1)
        btns.addWidget(self._back_btn)
        btns.addWidget(self._next_btn)
        root.addLayout(btns)
        self._sync_nav()
        return None

    def _go(self, delta: int) -> None:
        i = self._stack.currentIndex() + delta
        if i < 0:
            return None
        if i >= self._stack.count():
            self.accept()
            return None
        self._stack.setCurrentIndex(i)
        self._sync_nav()
        return None

    def _sync_nav(self) -> None:
        i = self._stack.currentIndex()
        n = self._stack.count()
        self._step_lbl.setText(f'Step {i + 1} of {n}')
        self._back_btn.setEnabled(i > 0)
        self._next_btn.setText('Got it' if i == n - 1 else 'Next  →')
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('sunk')))
        return None


class _TourOverlay(QWidget):
    """Translucent dim covering the parent window with an accent-bordered cut-out
    around each step's target, plus a callout panel with the description.

    Top-level frameless window (not a child of parent) so:
      1. WA_TranslucentBackground actually works -> cutout is truly see-through
         instead of black.
      2. Callouts can render outside the parent window bounds if needed.
    Geometry stays synced to the parent via an event filter on Move/Resize."""

    def __init__(self, parent, steps, on_done, on_finish_cta=None, final_cta_label: str = 'Done', final_secondary_label: str = 'Skip'):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.NoDropShadowWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(15))
        self._parent_win = parent
        self._steps = steps
        self._i = 0
        self._on_done = on_done
        self._hide_pending = None
        self._on_finish_cta = on_finish_cta
        self._final_label = final_cta_label
        self._final_secondary = final_secondary_label
        self._sync_geometry()
        parent.installEventFilter(self)
        self._callout = _TourCallout(self, self._next, self._skip)
        self._callout.show()
        self._apply_step()
        self.show()
        self.raise_()
        return None

    def _sync_geometry(self):
        scr = self._parent_win.screen() or QGuiApplication.primaryScreen()
        self.setGeometry(scr.geometry())
        return None

    def eventFilter(self, obj, e):
        if obj is self._parent_win:
            t = e.type()
            if t in (QEvent.Move, QEvent.Resize):
                self._sync_geometry()
                self._position_callout()
                self.update()
                return False
            if t == QEvent.Hide:
                self.hide()
                return False
            if t == QEvent.Show:
                self._sync_geometry()
                self._position_callout()
                self.show()
                self.raise_()
                return False
            if t == QEvent.WindowStateChange:
                if self._parent_win.isMinimized():
                    self.hide()
                    return False
                self._sync_geometry()
                self._position_callout()
                self.show()
                self.raise_()
                return False
            if t == QEvent.WindowDeactivate:
                from PySide6.QtWidgets import QApplication as _QA
                active = _QA.activeWindow()
                if active is self or active is self._parent_win:
                    return False
                if self._hide_pending is None:
                    self._hide_pending = QTimer(self)
                    self._hide_pending.setSingleShot(True)
                    self._hide_pending.setInterval(180)
                    self._hide_pending.timeout.connect(self.hide)
                self._hide_pending.start()
                return False
            if t == QEvent.WindowActivate:
                if self._hide_pending is not None:
                    self._hide_pending.stop()
                if not self.isVisible():
                    self._sync_geometry()
                    self._position_callout()
                    self.show()
                    self.raise_()
        return False

    def _target_rect(self) -> QRect:
        t = self._steps[self._i][0]
        widgets = t if isinstance(t, (list, tuple)) else [t]
        rect = QRect()
        for w in widgets:
            if w is None or not w.isVisible():
                continue
            tl_screen = w.mapToGlobal(QPoint(0, 0))
            tl_local = self.mapFromGlobal(tl_screen)
            r = QRect(tl_local, w.size())
            rect = r if rect.isNull() else rect.united(r)
        return rect

    def _apply_step(self):
        _, title, body = self._steps[self._i]
        self._callout.set_step(title, body, self._i, len(self._steps), final_label=self._final_label, secondary_label=self._final_secondary)
        self._position_callout()
        self.update()
        return None

    def _position_callout(self):
        target = self._target_rect()
        cw, ch = self._callout.sizeHint().width(), self._callout.sizeHint().height()
        m = _s(14)
        W, H = self.width(), self.height()
        candidates = [
            (target.center().x() - cw // 2, target.bottom() + m),
            (target.center().x() - cw // 2, target.top() - m - ch),
            (target.right() + m, target.center().y() - ch // 2),
            (target.left() - m - cw, target.center().y() - ch // 2),
        ]
        chosen = None
        for x, y in candidates:
            r = QRect(x, y, cw, ch)
            if x >= m and y >= m and x + cw + m <= W and y + ch + m <= H and not r.intersects(target):
                chosen = (x, y)
                break
        if chosen is None:
            x = max(m, min(W - cw - m, target.center().x() - cw // 2))
            y = max(m, min(H - ch - m, target.bottom() + m))
            chosen = (x, y)
        self._callout.setGeometry(chosen[0], chosen[1], cw, ch)
        self._callout.raise_()
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        win_screen = self._parent_win.frameGeometry()
        win_local = QRect(self.mapFromGlobal(win_screen.topLeft()), win_screen.size())
        p.fillRect(win_local, QColor(0, 0, 0, 150))
        target = self._target_rect()
        if not target.isValid() or target.isEmpty():
            return None
        r = QRectF(target.adjusted(-_s(6), -_s(6), _s(6), _s(6)))
        radius = _s(10)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.fillPath(path, Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        pen = QPen(QColor(_ACCENT))
        pen.setWidthF(3.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, radius, radius)
        return None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._position_callout()
        return None

    def closeEvent(self, e):
        if self._parent_win is not None:
            self._parent_win.removeEventFilter(self)
        super().closeEvent(e)
        return None

    def _next(self):
        if self._i + 1 >= len(self._steps):
            self._finish()
            return None
        self._i += 1
        self._apply_step()
        return None

    def _skip(self):
        last = self._i + 1 >= len(self._steps)
        cta = self._on_finish_cta if last else None
        self._finish()
        if cta:
            cta()
            return None
        return None

    def _finish(self):
        if self._parent_win is not None:
            self._parent_win.removeEventFilter(self)
        self.hide()
        self.deleteLater()
        self._on_done()
        return None


class _PreferencesDialog(QDialog):
    """App preferences, opened from the hamburger menu. v1: Appearance (theme
    switch). Room to grow (startup, troubleshooting). Emits `picked(name)` on
    every theme click and STAYS OPEN, so the caller can apply the theme live and
    the user can preview / switch again without reopening."""

    picked = Signal(str)

    def __init__(self, parent=None, cfg=None, ui=None):
        super().__init__(parent)
        self._ui = ui
        self._cfg = cfg
        if self._cfg is None:
            try:
                from fh6_spotify.config import Config, default_config_path
                self._cfg = Config.load(default_config_path())
            except Exception:
                self._cfg = None
        self.setWindowTitle('Preferences')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setMinimumWidth(_s(320))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(_s(18), _s(16), _s(18), _s(16))
        lay.setSpacing(_s(10))
        _hdr = QLabel('Appearance')
        _hdr.setFont(_ui_font(16, QFont.Bold))
        lay.addWidget(_hdr)
        _lbl = QLabel('Theme')
        _lbl.setObjectName('hint')
        _lbl.setFont(_ui_font(13))
        lay.addWidget(_lbl)
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        self._theme_btns = {}
        from PySide6.QtWidgets import QButtonGroup
        self._theme_grp = QButtonGroup(self)
        self._theme_grp.setExclusive(True)
        cur = _load_theme()
        for key, label in (('dark', 'Dark'), ('light', 'Light'), ('contrast', 'High contrast')):
            b = QPushButton(label)
            b.setObjectName('devbtn')
            b.setCheckable(True)
            b.setChecked(key == cur)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.picked.emit(k))
            self._theme_grp.addButton(b)
            row.addWidget(b, 1)
            self._theme_btns[key] = b
        lay.addLayout(row)
        from fh6_spotify.version import NO_CAROUSEL as _NO_CAROUSEL
        if not _NO_CAROUSEL:
            self._build_spotify_connect_row(lay)
        self._build_startup_row(lay)
        lay.addStretch(1)
        if hasattr(self, '_sp_status'):
            self._sp_poll = QTimer(self)
            self._sp_poll.setInterval(1500)
            self._sp_poll.timeout.connect(self._refresh_spotify_status)
            self._sp_poll.start()
            self._refresh_spotify_status()
            return None
        return None

    def _build_spotify_connect_row(self, parent_layout):
        from fh6_spotify import connect as _connect
        parent_layout.addSpacing(_s(7))
        _sep = QFrame()
        _sep.setFixedHeight(1)
        _sep.setStyleSheet(f"background: {_c('border')}; border: none;")
        parent_layout.addWidget(_sep)
        parent_layout.addSpacing(_s(8))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(_s(10))
        _sp_logo = QLabel()
        _sp_pm = QPixmap(_SPOTIFY)
        if not _sp_pm.isNull():
            _sp_logo.setPixmap(_sp_pm.scaled(_s(28), _s(28), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        _sp_logo.setFixedSize(_s(28), _s(28))
        _sp_logo.setAlignment(Qt.AlignVCenter | Qt.AlignHCenter)
        _sp_name = QLabel('Spotify')
        _sp_name.setFont(_ui_font(14, QFont.DemiBold))
        _sp_info = _InfoLabel('Lets the overlay scroll through your Spotify queue (previous and upcoming tracks) and skip from there. Premium required for skip control.')
        _name_row = QHBoxLayout()
        _name_row.setContentsMargins(0, 0, 0, 0)
        _name_row.setSpacing(_s(6))
        _name_row.addWidget(_sp_name)
        _name_row.addWidget(_sp_info)
        _name_row.addStretch()
        self._sp_status = QLabel('Not connected')
        self._sp_status.setObjectName('hint')
        self._sp_status.setFont(_ui_font(12))
        self._sp_status.setWordWrap(False)
        _text_col = QVBoxLayout()
        _text_col.setContentsMargins(0, 0, 0, 0)
        _text_col.setSpacing(_s(2))
        _text_col.addLayout(_name_row)
        _text_col.addWidget(self._sp_status)
        self._sp_connect = QPushButton('Connect Spotify')
        self._sp_connect.setObjectName('togglebtn')
        self._sp_connect.setCursor(Qt.PointingHandCursor)
        self._sp_disconnect = QPushButton('Disconnect')
        self._sp_disconnect.setObjectName('togglebtn')
        self._sp_disconnect.setCursor(Qt.PointingHandCursor)

        def _port():
            return getattr(self._cfg, 'canvas_service_port', 7355)

        def _save_cfg():
            try:
                from fh6_spotify.config import default_config_path
                self._cfg.save(default_config_path())
            except Exception:
                pass
            return None

        def _on_connect():
            self._cfg.connect_skip = True
            _save_cfg()
            if callable(getattr(self, '_ensure_canvas', None)):
                self._ensure_canvas()
            _connect.login(_port())
            self._sp_status.setText('Opening browser…')
            self._sp_status.setStyleSheet('color:%s;' % _c('text_dim'))
            self._sp_connect.setVisible(False)
            self._sp_disconnect.setVisible(False)
            return None

        def _on_disconnect():
            _connect.logout(_port())
            self._cfg.connect_skip = False
            _save_cfg()
            self._refresh_spotify_status()
            return None

        self._sp_connect.clicked.connect(_on_connect)
        self._sp_disconnect.clicked.connect(_on_disconnect)
        h.addWidget(_sp_logo)
        h.addLayout(_text_col, 1)
        h.addStretch()
        h.addWidget(self._sp_connect)
        h.addWidget(self._sp_disconnect)
        parent_layout.addWidget(row)
        self._refresh_spotify_status()
        return None

    def _refresh_spotify_status(self):
        if not hasattr(self, '_sp_status'):
            return None
        from fh6_spotify import connect as _connect
        port = getattr(self._cfg, 'canvas_service_port', 7355)
        h = _connect.health(port)
        if not h:
            self._sp_status.setText('Not connected')
            self._sp_status.setStyleSheet('color:%s;' % _c('text_dim'))
            self._sp_status.setTextFormat(Qt.PlainText)
            self._sp_disconnect.setEnabled(False)
            self._sp_disconnect.setVisible(False)
            self._sp_connect.setEnabled(True)
            self._sp_connect.setVisible(True)
            return None
        if h.get('auth'):
            if h.get('premium') is False:
                self._sp_status.setText("●  Connected<span style='color:%s;font-size:11px;'>  ·  Premium required for skip</span>" % _c('text_dim'))
                self._sp_status.setTextFormat(Qt.RichText)
            else:
                self._sp_status.setText('●  Connected')
                self._sp_status.setTextFormat(Qt.PlainText)
            self._sp_status.setStyleSheet('color:#1DB954;')
            self._sp_disconnect.setEnabled(True)
            self._sp_disconnect.setVisible(True)
            self._sp_connect.setEnabled(False)
            self._sp_connect.setVisible(False)
            return None
        running = (_connect.login_status(port) or {}).get('running')
        if running:
            self._sp_status.setText('Opening browser…')
            self._sp_connect.setVisible(False)
            self._sp_disconnect.setVisible(False)
        else:
            self._sp_status.setText('Not connected')
            self._sp_connect.setEnabled(True)
            self._sp_connect.setVisible(True)
            self._sp_disconnect.setVisible(False)
        self._sp_status.setTextFormat(Qt.PlainText)
        self._sp_status.setStyleSheet('color:%s;' % _c('text_dim'))
        self._sp_disconnect.setEnabled(False)
        return None

    def _build_startup_row(self, parent_layout):
        parent_layout.addSpacing(_s(7))
        _sep = QFrame()
        _sep.setFixedHeight(1)
        _sep.setStyleSheet(f"background: {_c('border')}; border: none;")
        parent_layout.addWidget(_sep)
        parent_layout.addSpacing(_s(8))
        _title = QLabel('Startup')
        _title.setFont(_ui_font(16, QFont.Bold))
        parent_layout.addWidget(_title)
        self._startup_cb = QCheckBox('Start with Windows')
        self._startup_cb.setCursor(Qt.PointingHandCursor)
        self._startup_cb.setToolTip('Launch Segue automatically when Windows starts (runs in the tray, no game needed).')
        try:
            self._startup_cb.setChecked(_autostart.installed_mode() == 'direct')
        except Exception:
            pass
        self._startup_cb.toggled.connect(self._on_start_with_windows)
        parent_layout.addWidget(self._startup_cb)
        self._autostart_cb = QCheckBox('Auto-start when a game launches')
        self._autostart_cb.setCursor(Qt.PointingHandCursor)
        self._autostart_cb.setToolTip('Launch Segue automatically when any of your games starts (Forza, Rocket League, or your picked game).')
        try:
            self._autostart_cb.setChecked(_autostart.installed_mode() == 'watch')
        except Exception:
            pass
        self._autostart_cb.toggled.connect(self._on_autostart)
        parent_layout.addWidget(self._autostart_cb)
        from fh6_spotify.version import KEYBOARD_SUMMON as _KB_SUMMON
        if _KB_SUMMON:
            self._kbsummon_cb = QCheckBox('Hold CapsLock to open Segue controls')
            self._kbsummon_cb.setCursor(Qt.PointingHandCursor)
            self._kbsummon_cb.setToolTip('Hold CapsLock to bring up the music controls (a keyboard alternative to a mouse side button). A normal tap still toggles Caps Lock.')
            try:
                self._kbsummon_cb.setChecked(bool(getattr(self._cfg, 'keyboard_summon', False)))
            except Exception:
                pass
            self._kbsummon_cb.toggled.connect(self._on_kb_summon)
            parent_layout.addWidget(self._kbsummon_cb)
            return None
        return None

    def _on_start_with_windows(self, on):
        try:
            if on:
                _autostart.install(direct=True)
            elif _autostart.installed_mode() == 'direct':
                _autostart.uninstall()
        except Exception:
            pass
        self._sync_startup_checks()
        return None

    def _sync_startup_checks(self):
        try:
            mode = _autostart.installed_mode()
            for cb, want in ((getattr(self, '_startup_cb', None), 'direct'), (getattr(self, '_autostart_cb', None), 'watch')):
                if cb is None:
                    continue
                cb.blockSignals(True)
                cb.setChecked(mode == want)
                cb.blockSignals(False)
        except Exception:
            pass

    def _on_autostart(self, on):
        try:
            if on:
                _autostart.install(direct=False)
            elif _autostart.installed_mode() == 'watch':
                _autostart.uninstall()
        except Exception:
            pass
        self._sync_startup_checks()
        return None

    def _on_kb_summon(self, on):
        from fh6_spotify.config import default_config_path
        self._cfg.keyboard_summon = bool(on)
        try:
            self._cfg.save(default_config_path())
        except Exception:
            pass
        try:
            ui = getattr(self, '_ui', None)
            if isinstance(ui, dict):
                ui['reinit_mouse'] = True
                return None
        except Exception:
            return None

    def refresh_selected(self, name: str) -> None:
        """Mark `name` as the active theme button (exclusive)."""
        for k, b in self._theme_btns.items():
            b.setChecked(k == name)
        return None


class SettingsWindow(QWidget):

    update_available_sig = Signal(object)
    update_progress_sig = Signal(int, int)
    update_ready_sig = Signal(str)
    update_failed_sig = Signal(str)
    def adjustSize(self):
        if self.isMaximized() or self.isFullScreen():
            return None
        super().adjustSize()
        return None

    def __init__(self, cfg, path, on_close, on_minimize=None, ui=None, on_scale=None, on_restart=None):
        super().__init__()
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        global _SCALE
        _SCALE = cfg.ui_scale if cfg.ui_scale in _SCALE_STEPS else _SCALE_STEPS[0]
        self._cfg = cfg
        _set_theme(_load_theme())
        self._path = path
        self._on_close = on_close
        self._quit_fn = on_close
        self._on_scale = on_scale
        self._on_restart = on_restart
        self._ui = ui
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().installEventFilter(self)
        except Exception:
            pass
        self._quitting = False
        self._frame_applied = False
        self._themed = []
        self._sliders = {}
        self._slider_vals = {}
        self._titlebar_h = _s(_TITLEBAR_H)
        self.setWindowTitle('Segue')
        if os.path.exists(_APP_ICON):
            self.setWindowIcon(QIcon(_APP_ICON))
        self.setStyleSheet(_build_qss(_CHECK))
        self.setFont(_ui_font(15))
        self._hint_font = _ui_font(14)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._save)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_titlebar())
        self._update_banner = self._build_update_banner()
        root.addWidget(self._update_banner)
        self.update_available_sig.connect(self._show_update_banner)
        self.update_progress_sig.connect(self._on_update_progress)
        self.update_ready_sig.connect(self._on_update_ready)
        self.update_failed_sig.connect(self._on_update_failed)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(_s(16), _s(10), _s(16), 0)
        bl.setSpacing(_s(9))
        body.setMaximumWidth(_s(720))
        body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        _bodywrap = QHBoxLayout()
        _bodywrap.setContentsMargins(0, 0, 0, 0)
        _bodywrap.addStretch(1)
        _bodywrap.addWidget(body, 1000)
        _bodywrap.addStretch(1)
        root.addLayout(_bodywrap)
        self._load_source_icons()
        self._link_was_linked = False
        self._link_glow_until = 0.0
        self._link_fade_start = 0.0
        self._link_blend_cache = {}
        status = _Card()
        self._status_card = status
        sh = QHBoxLayout(status)
        sh.setContentsMargins(_s(14), _s(9), _s(14), _s(9))
        sh.setSpacing(_s(12))
        self._conn_pane = QWidget()
        left = QVBoxLayout(self._conn_pane)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        icons = QHBoxLayout()
        icons.setSpacing(_s(5))
        _caret_col = _c('icon') if _active_theme() == 'light' else _c('icon_dim')
        self._caret_pm = _tinted(os.path.join(_ASSETS, 'down-chevron.png'), _caret_col, 12)
        self._game_hover_side = None
        self._forza_lbl = QPushButton()
        self._forza_lbl.setObjectName('srcseg')
        self._forza_lbl.setFocusPolicy(Qt.NoFocus)
        self._forza_lbl.setFont(_ui_font(11, QFont.Bold))
        self._forza_lbl.setIconSize(QSize(_s(26), _s(26)))
        self._forza_lbl.setFixedSize(_s(42), _s(40))
        self._forza_lbl.setFlat(True)
        self._forza_lbl.setCursor(Qt.PointingHandCursor)
        self._forza_lbl.setToolTip('Game - click to open it (launch or bring to front); use the chevron to switch game')
        self._forza_lbl._launchable = False
        self._forza_lbl._launch_glow = 0.4
        self._forza_lbl.clicked.connect(self._bring_game_to_front)
        self._forza_lbl.enterEvent = lambda e, b=self._forza_lbl: (QPushButton.enterEvent(b, e), setattr(b, '_launch_glow', 1.0), self._apply_game_icon(), self._set_game_side('icon'), b.update())
        self._forza_lbl.leaveEvent = lambda e, b=self._forza_lbl: (QPushButton.leaveEvent(b, e), setattr(b, '_launch_glow', 0.4), self._apply_game_icon(), self._game_side_recheck(), b.update())
        self._game_caret = QPushButton()
        self._game_caret.setObjectName('srcseg')
        self._game_caret.setProperty('menuopen', 'false')
        self._game_caret.setFocusPolicy(Qt.NoFocus)
        self._game_caret.setFixedSize(_s(22), _s(40))
        self._game_caret.setFlat(True)
        self._game_caret.setCursor(Qt.PointingHandCursor)
        self._game_caret.setToolTip('Switch game')
        self._game_caret.clicked.connect(self._game_menu)
        self._game_caret.paintEvent = lambda e, b=self._game_caret: self._paint_caret_btn(b, e, '_game_caret_angle', '_game_hover_side')
        self._game_caret.enterEvent = lambda e, b=self._game_caret: (QPushButton.enterEvent(b, e), self._set_game_side('caret'), b.update())
        self._game_caret.leaveEvent = lambda e, b=self._game_caret: (QPushButton.leaveEvent(b, e), self._game_side_recheck(), b.update())
        self._game_box = QFrame()
        self._game_box.setObjectName('srcbox')
        self._game_box.paintEvent = lambda e: self._paint_game_box(e)
        self._game_box.leaveEvent = lambda e: self._game_side_recheck()
        _gb = QHBoxLayout(self._game_box)
        _gb.setContentsMargins(0, 0, 0, 0)
        _gb.setSpacing(0)
        _gb.addWidget(self._game_caret)
        _gb.addWidget(self._forza_lbl)
        self._game_menu_closed_at = 0.0
        self._conn_lbl = QLabel()
        self._conn_lbl.setFixedSize(_s(46), _s(38))
        self._conn_lbl.setAlignment(Qt.AlignCenter)
        self._link_glow_fx = QGraphicsDropShadowEffect(self._conn_lbl)
        self._link_glow_fx.setColor(QColor('#1DB954'))
        self._link_glow_fx.setOffset(0, 0)
        self._link_glow_fx.setBlurRadius(0.0)
        self._conn_lbl.setGraphicsEffect(self._link_glow_fx)
        from PySide6.QtCore import QVariantAnimation
        self._link_entrance = QVariantAnimation(self)
        self._link_entrance.setDuration(4200)
        self._link_entrance.setStartValue(0.0)
        self._link_entrance.setKeyValueAt(0.1, 1.0)
        self._link_entrance.setKeyValueAt(0.32, 1.0)
        self._link_entrance.setEndValue(0.0)
        self._link_entrance.setEasingCurve(QEasingCurve.OutCubic)
        self._link_entrance.valueChanged.connect(self._on_link_glow)
        self._link_entrance.finished.connect(self._start_link_pulse)
        self._link_pulse = QVariantAnimation(self)
        self._link_pulse.setDuration(8500)
        self._link_pulse.setStartValue(0.0)
        self._link_pulse.setKeyValueAt(0.2, 0.5)
        self._link_pulse.setKeyValueAt(0.45, 0.0)
        self._link_pulse.setEndValue(0.0)
        self._link_pulse.setEasingCurve(QEasingCurve.InOutSine)
        self._link_pulse.setLoopCount(-1)
        self._link_pulse.valueChanged.connect(self._on_link_glow)
        self._src_hover_side = None
        self._spot_lbl = QPushButton()
        self._spot_lbl.setObjectName('srcseg')
        self._spot_lbl.setFocusPolicy(Qt.NoFocus)
        self._spot_lbl.setFont(_ui_font(11, QFont.Bold))
        self._spot_lbl.setIconSize(QSize(_s(26), _s(26)))
        self._spot_lbl.setFixedSize(_s(42), _s(40))
        self._spot_lbl.setFlat(True)
        self._spot_lbl.setCursor(Qt.PointingHandCursor)
        self._spot_lbl.setToolTip('Music source - click to open it; use the chevron to switch source')
        self._spot_lbl.clicked.connect(self._on_source_click)
        self._spot_lbl._launchable = False
        self._spot_lbl._launch_glow = 0.4
        self._spot_lbl.enterEvent = lambda e, b=self._spot_lbl: (QPushButton.enterEvent(b, e), setattr(b, '_launch_glow', 1.0), self._apply_source_icon(), self._set_src_side('icon'), b.update())
        self._spot_lbl.leaveEvent = lambda e, b=self._spot_lbl: (QPushButton.leaveEvent(b, e), setattr(b, '_launch_glow', 0.4), self._apply_source_icon(), self._src_side_recheck(), b.update())
        self._spot_caret = QPushButton()
        self._spot_caret.setObjectName('srcseg')
        self._spot_caret.setProperty('menuopen', 'false')
        self._spot_caret.setFocusPolicy(Qt.NoFocus)
        self._spot_caret.setFixedSize(_s(22), _s(40))
        self._spot_caret.setFlat(True)
        self._spot_caret.setCursor(Qt.PointingHandCursor)
        self._spot_caret.setToolTip('Switch music source')
        self._spot_caret.clicked.connect(self._on_source_caret_click)
        self._spot_caret.paintEvent = lambda e, b=self._spot_caret: self._paint_caret_btn(b, e, '_src_caret_angle')
        self._spot_caret.enterEvent = lambda e, b=self._spot_caret: (QPushButton.enterEvent(b, e), self._set_src_side('caret'), b.update())
        self._spot_caret.leaveEvent = lambda e, b=self._spot_caret: (QPushButton.leaveEvent(b, e), self._src_side_recheck(), b.update())
        self._src_box = QFrame()
        self._src_box.setObjectName('srcbox')
        self._src_box.paintEvent = lambda e: self._paint_src_box(e)
        self._src_box.leaveEvent = lambda e: self._src_side_recheck()
        _sb = QHBoxLayout(self._src_box)
        _sb.setContentsMargins(0, 0, 0, 0)
        _sb.setSpacing(0)
        _sb.addWidget(self._spot_lbl)
        _sb.addWidget(self._spot_caret)
        self._spot_menu_closed_at = 0.0
        icons.addStretch(1)
        icons.addWidget(self._game_box)
        icons.addWidget(self._conn_lbl)
        icons.addWidget(self._src_box)
        icons.addStretch(1)
        left.addStretch(1)
        left.addLayout(icons)
        left.addSpacing(_s(3))
        self._conn_text = QLabel('')
        self._conn_text.setObjectName('hint')
        self._conn_text.setFont(self._hint_font)
        self._conn_text.setAlignment(Qt.AlignCenter)
        self._conn_text.setWordWrap(True)
        self._conn_text.setMinimumHeight(QFontMetrics(self._hint_font).height() + _s(2))
        left.addWidget(self._conn_text)
        left.addStretch(1)
        _cap_h = QFontMetrics(self._hint_font).height() + _s(2)
        self._conn_pane.setMinimumHeight(_s(40) + _cap_h + _s(10))
        sh.addWidget(self._conn_pane, 1)
        div = QFrame()
        div.setObjectName('vline')
        div.setFixedWidth(1)
        sh.addWidget(div)
        self._cover_sz = _s(48)
        self._np_area = QWidget()
        np = QHBoxLayout(self._np_area)
        np.setContentsMargins(0, 0, 0, 0)
        np.setSpacing(_s(10))
        self._cover = QLabel()
        self._cover.setFixedSize(self._cover_sz, self._cover_sz)
        self._cover.setPixmap(_cover_placeholder(self._cover_sz, _s(8)))
        np.addWidget(self._cover)
        meta = QVBoxLayout()
        meta.setSpacing(_s(2))
        meta.addStretch(1)
        self._np_title = QLabel('Nothing playing')
        self._np_title.setObjectName('nptitle')
        self._np_title.setFont(_ui_font(14, QFont.Bold))
        self._np_artist = QLabel('')
        self._np_artist.setObjectName('hint')
        self._np_artist.setFont(self._hint_font)
        self._np_clickable = False
        self._np_raw_title = ''
        self._np_raw_artist = ''
        self._np_title.mousePressEvent = lambda e: self._open_spotify_link('album')
        self._np_artist.mousePressEvent = lambda e: self._open_spotify_link('artist')
        for _npl in (self._np_title, self._np_artist):
            _npl.enterEvent = lambda e, w=_npl: self._np_hover(w, True)
            _npl.leaveEvent = lambda e, w=_npl: self._np_hover(w, False)
        artist_row = QHBoxLayout()
        artist_row.setSpacing(_s(8))
        artist_row.addWidget(self._np_artist, 1)
        self._bars = _PlayingBars()
        self._bars.setCursor(Qt.PointingHandCursor)
        self._bars.setToolTip('Open the Visualizer')
        self._bars.mousePressEvent = lambda e: self._open_visualizer()
        artist_row.addWidget(self._bars, 0, Qt.AlignBottom)
        meta.addWidget(self._np_title)
        meta.addLayout(artist_row)
        meta.addStretch(1)
        np.addLayout(meta, 1)
        sh.addWidget(self._np_area, 1)
        self._last_thumb = None
        bl.addWidget(status)
        self._active_tab = None
        row = QHBoxLayout()
        row.setSpacing(_s(8))
        _TI = 20
        _DULL = _c('icon_dim')
        self._ic_mixer = _tab_icon('mixer', _TI)
        self._ic_mixer_dull = _tab_icon('mixer', _TI, _DULL)
        self._ic_extras = _tab_icon('extras', _TI)
        self._ic_extras_dull = _tab_icon('extras', _TI, _DULL)
        self._tab_mixer = QPushButton()
        self._tab_mixer.setObjectName('tabbtn')
        self._tab_mixer.setIconSize(QSize(_s(_TI), _s(_TI)))
        self._tab_mixer.setToolTip('Mixer - volume levels')
        self._tab_mixer.setCursor(Qt.PointingHandCursor)
        self._tab_mixer.clicked.connect(lambda: self._set_tab('mixer'))
        self._tab_extras = QPushButton()
        self._tab_extras.setObjectName('tabbtn')
        self._tab_extras.setIconSize(QSize(_s(_TI), _s(_TI)))
        self._tab_extras.setToolTip('Overlay')
        self._tab_extras.setCursor(Qt.PointingHandCursor)
        self._tab_extras.clicked.connect(lambda: self._set_tab('extras'))
        _M, _PM = 22, 30
        _src_ui = self._ui or {}

        def _go(key, fallback):
            cb = _src_ui.get(key)
            if cb:
                return cb
            return fallback
        self._btn_prev = self._media_btn('prev', lambda: _go('routed_prev', _mk.media_prev)(), _M)
        self._btn_play = self._media_btn('play', lambda: _go('routed_playpause', _mk.media_playpause)(), _PM, white=True)
        self._btn_next = self._media_btn('next', lambda: _go('routed_next', _mk.media_next)(), _M)
        self._pm_size = _PM
        self._load_play_icons()
        self._btn_play.setIcon(self._icon_play)
        for b in (self._tab_mixer, self._tab_extras, self._btn_prev, self._btn_play, self._btn_next):
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            row.addWidget(b, 1, Qt.AlignTop)
        tabs_wrap = QVBoxLayout()
        tabs_wrap.setSpacing(0)
        tabs_wrap.addLayout(row)
        bl.addLayout(tabs_wrap)
        mixer = _Card()
        mixer.setObjectName('tabpanelL')
        self._mixer_card = mixer
        ml = QVBoxLayout(mixer)
        ml.setContentsMargins(_s(14), _s(12), _s(14), _s(12))
        ml.setSpacing(_s(10))
        ml.addWidget(self._panel_title('Mixer'))
        _v = (self._ui or {}).get('volume')
        if _v is None:
            _v = self._cfg.full_level
        _init_vol = int(round(_v * 100))
        ml.addWidget(self._slider_row('Volume', 0, 100, _init_vol, self._on_volume, tip='Master music volume - the same one your volume buttons change. Applies right away.', key='vol'))
        ml.addWidget(self._slider_row('Unfocused Volume', 0, 100, int(self._cfg.unfocused_level * 100), self._on_unfocused, tip="Music volume when the game window isn't focused (alt-tabbed, minimised, etc.). 0 = muted.", key='unfocused'))
        _menu_row = self._slider_row('Menu/Pause Volume', 0, 100, int(self._cfg.menu_level * 100), self._on_menu, tip='Music volume in menus and when paused. 0 = muted.', key='menu')
        ml.addWidget(_menu_row)
        self._mixer_adv_open = False
        self._mixer_adv_btn = QPushButton('Speech recognition')
        _style_expander(self._mixer_adv_btn, lambda: self._mixer_adv_open)
        self._themed.append(lambda: _style_expander(self._mixer_adv_btn, lambda: self._mixer_adv_open))
        self._mixer_adv_btn.setToolTip('Ducked volume, speech detection + fade')
        self._mixer_adv_btn.clicked.connect(self._toggle_mixer_adv)
        ml.addWidget(self._mixer_adv_btn)
        self._duck_row = self._slider_row('Ducked Volume', 0, 100, int(self._cfg.duck_level * 100), self._on_duck, tip="Music volume while someone talks in-game.\nSpeech detection isn't perfect - it can be a little wonky. Tune the fade in Extras.", key='duck')
        ml.addWidget(self._duck_row)
        self._duck_cb = QCheckBox()
        self._duck_cb.setChecked(self._cfg.ducking_enabled)
        self._duck_cb.setText('Enabled' if self._duck_cb.isChecked() else 'Enable')
        self._duck_cb.toggled.connect(lambda on, c=self._duck_cb: c.setText('Enabled' if on else 'Enable'))
        self._duck_cb.setCursor(Qt.PointingHandCursor)
        self._duck_cb.setToolTip('Automatically lower music when speech is detected')
        self._duck_cb.toggled.connect(self._on_ducking)
        self._lowcpu_cb = QCheckBox('Save CPU')
        self._lowcpu_cb.setChecked(False)
        self._lowcpu_cb.setEnabled(False)
        self._lowcpu_cb.setToolTip('')
        self._lowcpu_cb.toggled.connect(self._on_lowcpu)
        self._lowcpu_cb.setVisible(False)
        self._cpu_label = QLabel('')
        self._cpu_label.setObjectName('hint')
        self._cpu_label.setFont(self._hint_font)
        self._cpu_label.setVisible(False)
        _speech_row = QWidget()
        duck_row = QHBoxLayout(_speech_row)
        duck_row.setContentsMargins(0, 0, 0, 0)
        duck_row.setSpacing(_s(12))
        duck_row.addWidget(self._duck_cb)
        self._duckscope_cb = QCheckBox('Include Discord')
        self._duckscope_cb.setChecked(getattr(self._cfg, 'duck_scope', 'game') == 'system')
        self._duckscope_cb.setCursor(Qt.PointingHandCursor)
        self._duckscope_cb.setToolTip("Also duck under other apps' audio (Discord voice chat, etc.),\nnot just the game. Your music app is excluded, so the song itself\nwon't trigger ducking. Uses a bit more CPU (listens continuously).")
        self._duckscope_cb.toggled.connect(self._on_duck_scope)
        duck_row.addWidget(self._duckscope_cb)
        duck_row.addStretch(1)
        duck_row.addWidget(self._lowcpu_cb)
        duck_row.addWidget(self._cpu_label)
        ml.addWidget(_speech_row)
        self._sens_row = self._slider_row('Speech sensitivity', 0, 100, _thresh_to_sens(self._cfg.vad_threshold), self._on_sensitivity, tip='How eagerly Segue ducks for speech.\nHigher = ducks on quieter or less-certain audio.\nLower = only clear speech.\nRaise it if it misses talking, lower it if it ducks on engine / SFX.', key='sens')
        ml.addWidget(self._sens_row)
        self._mic_row = QWidget()
        _micl = QHBoxLayout(self._mic_row)
        _micl.setContentsMargins(0, 0, 0, 0)
        _micl.setSpacing(_s(8))
        self._ownvoice_cb = QCheckBox('Include self')
        self._ownvoice_cb.setChecked(getattr(self._cfg, 'duck_on_own_voice', False))
        self._ownvoice_cb.setCursor(Qt.PointingHandCursor)
        self._ownvoice_cb.setToolTip("Include self: in a voice chat, also keep the music ducked while YOU\ntalk, so it doesn't jump back up between your sentences. Only kicks in\nonce a friend has spoken (talking with no one else active won't duck).\nUses the mic on the right - opened only during an active conversation,\nnever stored.")
        self._ownvoice_cb.toggled.connect(self._on_own_voice)
        _micl.addWidget(self._ownvoice_cb)
        _micl.addSpacing(_s(12))
        _mic_lbl = QLabel()
        _mic_lbl.setPixmap(_mic_pixmap(15, _c('text_hint')))
        _mic_lbl.setToolTip('Which mic counts as your voice')
        self._mic_lbl = _mic_lbl
        _micl.addWidget(_mic_lbl)
        self._mic_combo = QComboBox()
        self._mic_combo.setCursor(Qt.PointingHandCursor)
        self._mic_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._mic_combo.setMinimumContentsLength(12)
        self._mic_combo.setToolTip("Which mic counts as YOUR voice for 'Duck on my voice'. Set it to the\nsame mic Discord uses - often NOT the Windows default, since a\nplugged-in controller hijacks the default input.")
        try:
            from fh6_spotify.mic_capture import MicCapture
            self._mic_names = MicCapture.list_input_devices()
        except Exception:
            self._mic_names = []
        self._mic_combo.addItem('Windows default')
        for _nm in self._mic_names:
            self._mic_combo.addItem(_nm)
        _cur_mic = getattr(self._cfg, 'mic_device', '') or ''
        _sel = 0
        if _cur_mic:
            for _i, _nm in enumerate(self._mic_names):
                if _cur_mic.lower() in _nm.lower():
                    _sel = _i + 1
                    break
        self._mic_combo.setCurrentIndex(_sel)
        self._mic_combo.currentIndexChanged.connect(self._on_mic_device)
        _micl.addWidget(self._mic_combo, 1)
        ml.addWidget(self._mic_row)
        _fade_row = self._slider_row('Fade length', 100, 2000, _ms_from_ramp(self._cfg.volume_ramp_in), self._on_fade, tip='How long music takes to fade in when it returns.', key='fade', fmt=lambda x: f'{x} ms')
        ml.addWidget(_fade_row)
        self._adv_rows = [
            ('ducked_volume', self._duck_row),
            ('speech_recognition', _speech_row),
            ('speech_recognition', self._sens_row),
            ('speech_recognition', self._mic_row),
            ('fade_length', _fade_row),
        ]
        self._duck_row.setEnabled(self._cfg.ducking_enabled)
        self._sens_row.setEnabled(self._cfg.ducking_enabled)
        self._duckscope_cb.setEnabled(self._cfg.ducking_enabled)
        self._ownvoice_cb.setEnabled(self._cfg.ducking_enabled and getattr(self._cfg, 'duck_scope', 'game') == 'system')
        self._mic_combo.setEnabled(self._ownvoice_cb.isEnabled() and self._ownvoice_cb.isChecked())
        self._refresh_adv_rows()
        tabs_wrap.addWidget(mixer)
        self._adv_card = _Card()
        self._adv_card.setObjectName('tabpanel')
        al = QVBoxLayout(self._adv_card)
        al.setContentsMargins(_s(14), _s(12), _s(14), _s(12))
        al.setSpacing(_s(10))
        al.addWidget(self._panel_title('Overlay'))
        _LEFTW = _s(140)
        self._overlay_cb = QCheckBox()
        self._overlay_cb.setChecked(self._cfg.overlay_enabled)
        self._overlay_cb.setText('Enabled' if self._overlay_cb.isChecked() else 'Enable')
        self._overlay_cb.toggled.connect(lambda on, c=self._overlay_cb: c.setText('Enabled' if on else 'Enable'))
        self._overlay_cb.setCursor(Qt.PointingHandCursor)
        self._overlay_cb.setToolTip('Show the now-playing overlay in-game')
        self._overlay_cb.toggled.connect(self._on_overlay)
        self._only_cover_cb = QCheckBox('Cover only')
        self._only_cover_cb.setChecked(self._cfg.overlay_compact)
        self._only_cover_cb.setCursor(Qt.PointingHandCursor)
        self._only_cover_cb.setToolTip('Shrink the overlay to just the album cover')
        self._only_cover_cb.toggled.connect(self._on_only_cover)
        self._always_on_cb = QCheckBox('Always on')
        self._always_on_cb.setChecked(self._cfg.overlay_always_on)
        self._always_on_cb.setCursor(Qt.PointingHandCursor)
        self._always_on_cb.setToolTip('Keep the overlay (album art) visible instead of fading out when nothing changes')
        self._always_on_cb.toggled.connect(self._on_overlay_always)
        self._ingame_cb = QCheckBox('Only in game')
        self._ingame_cb.setChecked(getattr(self._cfg, 'overlay_in_game_only', False))
        self._ingame_cb.setCursor(Qt.PointingHandCursor)
        self._ingame_cb.setToolTip('Only show the overlay while the game window is focused.\nHides it on the desktop / when you alt-tab out.')
        self._ingame_cb.toggled.connect(self._on_overlay_ingame)
        self._drive_cb = QCheckBox('Drive only')
        self._drive_cb.setChecked(getattr(self._cfg, 'overlay_drive_only', False))
        self._drive_cb.setCursor(Qt.PointingHandCursor)
        self._drive_cb.setToolTip("Only show the overlay while you're actually driving.\nHides it in menus, the garage, car shows, pauses - anywhere track-skipping is unavailable.")
        self._drive_cb.toggled.connect(self._on_overlay_drive)
        _fm_cb = QFontMetrics(self.font())
        _aon_pad = max(0, _fm_cb.horizontalAdvance('Only in game') - _fm_cb.horizontalAdvance('Always on'))
        toggles_row = QHBoxLayout()
        toggles_row.setSpacing(_s(14))
        _ov_cell = QWidget()
        _ov_cell.setFixedWidth(_LEFTW)
        _ovh = QHBoxLayout(_ov_cell)
        _ovh.setContentsMargins(0, 0, 0, 0)
        _ovh.addWidget(self._overlay_cb)
        _ovh.addStretch(1)
        toggles_row.addWidget(_ov_cell)
        _right_toggles = QHBoxLayout()
        _right_toggles.setContentsMargins(0, 0, 0, 0)
        _right_toggles.addWidget(self._only_cover_cb, 0, Qt.AlignLeft)
        _right_toggles.addStretch(1)
        _right_toggles.addWidget(self._always_on_cb, 0, Qt.AlignVCenter)
        _right_toggles.addSpacing(_aon_pad)
        toggles_row.addLayout(_right_toggles, 1)
        al.addLayout(toggles_row)
        al.addSpacing(_s(7))
        _ov_sep = QFrame()
        _ov_sep.setFixedHeight(1)
        _ov_sep.setStyleSheet(f'background: {_c("border")}; border: none;')
        self._themed.append(lambda w=_ov_sep: w.setStyleSheet(f'background: {_c("border")}; border: none;'))
        al.addWidget(_ov_sep)
        al.addSpacing(_s(9))
        self._overlay_moving = False
        self._move_sz = 20
        self._move_overlay_btn = QPushButton()
        self._move_overlay_btn.setObjectName('resetbtn')
        self._move_overlay_btn.setCursor(Qt.PointingHandCursor)
        self._move_overlay_btn.setIcon(QIcon(_move_pixmap(self._move_sz, _c('text_hint'))))
        self._move_overlay_btn.setIconSize(QSize(_s(self._move_sz), _s(self._move_sz)))
        self._move_overlay_btn.setFixedSize(_s(30), _s(30))
        self._move_overlay_btn.setToolTip('Move the overlay: grab and drag it anywhere on screen, then click again to finish. (Tip: double-click the overlay to move it too.)')
        self._move_overlay_btn.clicked.connect(self._on_move_overlay)
        self._overlay_pos_picker = _OverlayPositionPicker(self._cfg.overlay_position, self._cfg.overlay_custom_x, self._cfg.overlay_custom_y)
        self._overlay_pos_picker.changed.connect(self._on_overlay_pos)
        self._overlay_pos_picker.custom_changed.connect(self._on_overlay_custom)
        size_top = QHBoxLayout()
        size_top.setSpacing(_s(8))
        size_lbl = QLabel('Size')
        size_lbl.setObjectName('hint')
        size_lbl.setFont(self._hint_font)
        self._overlay_size_val = QLabel(f'{int(round(self._cfg.overlay_scale * 100))}%')
        self._overlay_size_val.setObjectName('hint')
        self._overlay_size_val.setFont(self._hint_font)
        size_top.addWidget(size_lbl)
        size_top.addStretch(1)
        size_top.addWidget(self._overlay_size_val)
        self._overlay_size_slider = _ResettableSlider(Qt.Horizontal, default_value=100)
        self._overlay_size_slider.setRange(60, 200)
        self._overlay_size_slider.setValue(int(round(self._cfg.overlay_scale * 100)))
        self._overlay_size_slider.setCursor(Qt.PointingHandCursor)
        self._overlay_size_slider.setToolTip('Double-click to reset to 100%')
        self._overlay_size_slider.valueChanged.connect(self._on_overlay_size)
        self._overlay_size_reset = QPushButton()
        self._overlay_size_reset.setObjectName('resetbtn')
        self._overlay_size_reset.setCursor(Qt.PointingHandCursor)
        self._overlay_size_reset.setToolTip('Reset overlay size to 100%')
        _undo_sz = 20
        self._overlay_size_reset.setIcon(QIcon(_undo_pixmap(_undo_sz, _c('text_hint'))))
        self._overlay_size_reset.setIconSize(QSize(_s(_undo_sz), _s(_undo_sz)))
        self._overlay_size_reset.setFixedSize(_s(30), _s(30))
        sp = self._overlay_size_reset.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self._overlay_size_reset.setSizePolicy(sp)
        self._overlay_size_reset.clicked.connect(lambda: self._overlay_size_slider.setValue(100))
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(_s(8))
        btn_row.addWidget(self._move_overlay_btn, 0, Qt.AlignVCenter)
        btn_row.addStretch(1)
        btn_row.addWidget(self._overlay_size_reset, 0, Qt.AlignVCenter)
        size_col = QVBoxLayout()
        size_col.setContentsMargins(0, _s(4), 0, 0)
        size_col.setSpacing(_s(6))
        size_col.addLayout(size_top)
        size_col.addWidget(self._overlay_size_slider)
        size_col.addStretch(1)
        size_col.addLayout(btn_row)
        pos_row = QHBoxLayout()
        pos_row.setSpacing(_s(14))
        pos_row.addWidget(self._overlay_pos_picker, 0, Qt.AlignLeft | Qt.AlignTop)
        pos_row.addLayout(size_col, 1)
        al.addLayout(pos_row)
        self._overlay_size_reset.setVisible(self._cfg.overlay_scale != 1.0)
        self._update_overlay_subs_enabled()
        autostart_row = QHBoxLayout()
        autostart_row.setContentsMargins(0, _s(6), 0, 0)
        autostart_row.addWidget(self._drive_cb)
        autostart_row.addStretch(1)
        autostart_row.addWidget(self._ingame_cb, 0, Qt.AlignRight)
        al.addLayout(autostart_row)
        self._game_row = QWidget()
        gh = QHBoxLayout(self._game_row)
        gh.setContentsMargins(0, 0, 0, 0)
        gh.setSpacing(_s(8))
        self._game_lbl = QLabel('Game:')
        self._game_lbl.setObjectName('hint')
        self._game_lbl.setFont(self._hint_font)
        self._game_name_lbl = QLabel(self._cfg.general_target_process or '(not picked)')
        self._game_name_lbl.setFont(self._hint_font)
        pick = QPushButton('Pick game')
        pick.setObjectName('togglebtn')
        pick.setCursor(Qt.PointingHandCursor)
        pick.clicked.connect(self._pick_game)
        gh.addWidget(self._game_lbl)
        gh.addWidget(self._game_name_lbl, 1)
        gh.addWidget(pick)
        self._game_row.setVisible(self._cfg.mode == 'general')
        al.addWidget(self._game_row)
        self._adv_card.setVisible(False)
        tabs_wrap.addWidget(self._adv_card)
        ctl = QHBoxLayout()
        ctl.setSpacing(_s(8))
        _dev = self._cfg.input_device
        _dev_label = _dev_name(_dev)
        rebind_btn = QPushButton()
        self._controls_btn = rebind_btn
        rebind_btn.setObjectName('togglebtn')
        rebind_btn.setCursor(Qt.PointingHandCursor)
        rebind_btn.setToolTip(f'Using {_dev_label}. Click to view all controls and rebind buttons.')
        _rl = QHBoxLayout(rebind_btn)
        _rl.setContentsMargins(_s(14), 0, _s(10), 0)
        _rl.setSpacing(_s(5))
        _rtxt = QLabel('Rebind controls')
        _rtxt.setFont(self._hint_font)
        _rtxt.setStyleSheet(f'color: {_c("text")}; background: transparent;')
        self._themed.append(lambda w=_rtxt: w.setStyleSheet(f'color: {_c("text")}; background: transparent;'))
        _rdot = QLabel('·')
        _rdot.setFont(_ui_font(21, QFont.Bold))
        _rdot.setStyleSheet(f'color: {_c("text_hint")}; background: transparent;')
        self._themed.append(lambda w=_rdot: w.setStyleSheet(f'color: {_c("text_hint")}; background: transparent;'))
        _rdev_font = QFont(self._hint_font)
        _rdev_font.setBold(True)
        _rdev = QLabel(_dev_label)
        _rdev.setFont(_rdev_font)
        _rdev.setStyleSheet(f'color: {_ACCENT}; background: transparent;')
        self._rdev_lbl = _rdev
        for _w in (_rtxt, _rdot, _rdev):
            _w.setAttribute(Qt.WA_TransparentForMouseEvents)
        _rl.addStretch(1)
        _rl.addSpacing(_s(26))
        for _w in (_rtxt, _rdot, _rdev):
            _rl.addWidget(_w)
        _rl.addSpacing(_s(72))
        _rl.addStretch(1)
        self._pill_text_w = QFontMetrics(self._hint_font).horizontalAdvance('Rebind controls') + _s(5) + QFontMetrics(_ui_font(21, QFont.Bold)).horizontalAdvance('·') + _s(5) + QFontMetrics(_rdev_font).horizontalAdvance(_dev_label)
        self._dev_wm_big = _dev_wm_rot(_dev)
        rebind_btn.paintEvent = lambda e, b=rebind_btn: self._paint_controls_pill(b, e)
        rebind_btn.clicked.connect(self._open_rebind)
        self._attach_pill_press(rebind_btn, (_rtxt, _rdot, _rdev))
        self._lock_btn = _ToggleButton('', 'lock')
        self._lock_btn.setIconSize(QSize(_s(19), _s(19)))
        _unl = _tinted(os.path.join(_ASSETS, 'unlocked lock.png'), _c('text_hint'), 19)
        self._lock_btn._off_icon = QIcon(_unl) if _unl is not None else _kind_icon('lock', _c('text_hint'))
        self._lock_btn.setIcon(self._lock_btn._off_icon)
        self._lock_btn.setToolTip('Lock track-skipping (also toggled by the controller)')
        self._lock_btn.clicked.connect(self._toggle_lock)
        self._power_btn = _ToggleButton('', 'power', object_name='powerbtn')
        self._power_btn.setToolTip("Turn Segue's control on / off (without closing it)")
        self._power_btn.clicked.connect(self._toggle_disabled)
        self._viz_btn = QPushButton()
        self._viz_btn.setObjectName('togglebtn')
        self._viz_btn.setCursor(Qt.PointingHandCursor)
        self._viz_btn.setToolTip('Open the Visualizer')
        self._viz_btn.setFixedWidth(_s(46))
        _vz_pm = QPixmap(_s(40), _s(40))
        _vz_pm.fill(QColor(0, 0, 0, 0))
        _vp = QPainter(_vz_pm)
        _vp.setRenderHint(QPainter.Antialiasing)
        _vp.setPen(Qt.NoPen)
        _vp.setBrush(QColor(_c('icon')))
        _bw_ = _s(4)
        for _i, _bh in enumerate((0.38, 0.72, 0.52, 0.86, 0.46)):
            _x = _s(4) + _i * (_bw_ + _s(3))
            _hh = int(_s(40) * _bh * 0.78)
            _vp.drawRoundedRect(_x, (_s(40) - _hh) // 2, _bw_, _hh, _bw_ * 0.45, _bw_ * 0.45)
        _vp.end()
        self._viz_btn.setIcon(QIcon(_vz_pm))
        self._viz_btn.setIconSize(QSize(_s(20), _s(20)))
        self._viz_btn.clicked.connect(self._open_visualizer)
        self._viz_badge = QLabel('New', self)
        self._viz_badge.setAttribute(Qt.WA_StyledBackground, True)
        self._viz_badge.setStyleSheet(f'background:#F26B1D; color:#141414; font-weight:800; font-size:{_s(9)}px; border-radius:{_s(6)}px; padding:{_s(1)}px {_s(6)}px;')
        self._viz_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._viz_badge.adjustSize()
        self._viz_btn.setVisible(False)
        self._viz_badge.setVisible(False)
        ctl.addWidget(self._lock_btn)
        ctl.addWidget(rebind_btn, 1)
        ctl.addWidget(self._viz_btn)
        ctl.addWidget(self._power_btn)
        bl.addLayout(ctl)
        for _b in (self._menu_btn, self._btn_prev, self._btn_play, self._btn_next, self._tab_mixer, self._tab_extras, self._lock_btn, self._power_btn, self._viz_btn):
            self._attach_press_bounce(_b)
        self._src_caret_angle = 0.0
        self._game_caret_angle = 0.0
        self._attach_press_bounce(self._spot_lbl)
        self._attach_press_bounce(self._forza_lbl)
        self._update_tabs()
        foot = QVBoxLayout()
        foot.setSpacing(_s(6))
        _kofi_pm = _load_scaled(os.path.join(_ASSETS, 'kofi_logo.png'), 16)
        _disc_pm = _tinted(os.path.join(_ASSETS, 'Discord_logo_blue.png'), _c('icon'), 16)

        def _foot_html(hover_url=None):
            kd = 'underline' if hover_url == _SUPPORT_URL else 'none'
            dd = 'underline' if hover_url == _DISCORD_URL else 'none'
            return f'<a href="{_SUPPORT_URL}" style="color:{_c("text")}; text-decoration:{kd};"><b>Support me</b></a><span style="color:{_c("text_dim")};"> if you wanna</span>&nbsp;&nbsp;·&nbsp;&nbsp;<a href="{_DISCORD_URL}" style="color:{_c("text")}; text-decoration:{dd};"><b>Join the Discord</b></a>'
        srow = QHBoxLayout()
        srow.setSpacing(_s(6))
        srow.addStretch(1)
        if _kofi_pm is not None:
            _k = QLabel()
            _k.setPixmap(_kofi_pm)
            srow.addWidget(_k, 0, Qt.AlignVCenter)
        _flbl = QLabel(_foot_html())
        _flbl.setObjectName('hint')
        _flbl.setFont(self._hint_font)
        _flbl.setTextFormat(Qt.RichText)
        _flbl.setWordWrap(False)
        _flbl.setOpenExternalLinks(True)
        _flbl.linkHovered.connect(lambda u: (_flbl.setText(_foot_html(u or None)), _flbl.setCursor(Qt.PointingHandCursor if u else Qt.ArrowCursor)))
        self._foot_html = _foot_html
        self._foot_lbl = _flbl
        srow.addWidget(_flbl, 0, Qt.AlignVCenter)
        self._foot_disc = None
        if _disc_pm is not None:
            _d = QLabel()
            _d.setPixmap(_disc_pm)
            srow.addWidget(_d, 0, Qt.AlignVCenter)
            self._foot_disc = _d
        srow.addStretch(1)
        foot.addLayout(srow)
        bl.addLayout(foot)
        bl.addSpacing(_s(44))
        bl.addStretch(1)
        self._brand_logo = QPixmap(os.path.join(_ASSETS, 'Segue logo in bottom ui.png'))
        self.setMinimumWidth(_s(460))
        self.resize(self.sizeHint())
        self._refresh_status()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(100)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        return None


    def showEvent(self, e):
        super().showEvent(e)
        if sys.platform == 'win32':
            if not self._frame_applied:
                self._frame_applied = True
                hwnd = int(self.winId())
                self._hwnd = hwnd
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 39)
                try:
                    from fh6_spotify.taskbar_buttons import TaskbarButtons
                    self._taskbar = TaskbarButtons(hwnd, self._taskbar_action)
                except Exception:
                    self._taskbar = None
                QTimer.singleShot(1500, lambda: self._taskbar and self._taskbar.add('late'))
                QTimer.singleShot(0, self._fit_window)
                QTimer.singleShot(50, self.update)
                QTimer.singleShot(180, self._maybe_run_tour)
                QTimer.singleShot(120, self._place_viz_badge)
                QTimer.singleShot(0, self._apply_preset_visibility)
                QTimer.singleShot(0, self._kill_ui_focus)
                QTimer.singleShot(3000, self._kick_update_check)
                self._update_poll = QTimer(self)
                self._update_poll.setInterval(7200000)
                self._update_poll.timeout.connect(self._kick_update_check)
                self._update_poll.start()
                return None
            return None
        return None

    def _build_update_banner(self) -> QWidget:
        """Slim orange row under the titlebar. Hidden until updater finds a
        newer release. Click anywhere -> open Ko-fi page; click × -> dismiss
        for this version only (next version will surface a fresh banner)."""
        w = QWidget()
        w.setObjectName('updatebanner')
        w.setVisible(False)
        h = QHBoxLayout(w)
        h.setContentsMargins(_s(14), _s(6), _s(8), _s(6))
        h.setSpacing(_s(3))
        spark = QLabel()
        spark.setObjectName('updatebannerspark')
        spark.setPixmap(_update_pixmap(14))
        spark.setAlignment(Qt.AlignVCenter)
        self._ub_spark = spark
        badge = QLabel('UPDATE')
        badge.setObjectName('updatebannertext')
        _bf = _ui_font(13, QFont.Black)
        try:
            _bf.setLetterSpacing(QFont.PercentageSpacing, 100)
        except Exception:
            pass
        badge.setFont(_bf)
        badge.setAlignment(Qt.AlignVCenter)
        self._ub_badge = badge
        lbl = _FadeLabel('Update available')
        lbl.setObjectName('updatebannertext')
        lbl.setFont(_ui_font(13, QFont.DemiBold))
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        whatsnew = QPushButton("What's new")
        whatsnew.setObjectName('updatebannerlink')
        whatsnew.setCursor(Qt.PointingHandCursor)
        whatsnew.setFont(_ui_font(12, QFont.Bold))
        whatsnew.clicked.connect(lambda: self._show_whats_new())
        whatsnew.setVisible(False)
        link = QPushButton('Get it →')
        link.setObjectName('updatebannerbtn')
        link.setCursor(Qt.PointingHandCursor)
        link.setFont(_ui_font(12, QFont.Bold))
        link.clicked.connect(self._on_update_banner_click)
        later = QPushButton('Later')
        later.setObjectName('updatebannerlink')
        later.setCursor(Qt.PointingHandCursor)
        later.setFont(_ui_font(12, QFont.Bold))
        later.clicked.connect(self._on_update_later)
        later.setVisible(False)
        self._ub_later = later
        close = QPushButton()
        close.setObjectName('updatebannerclose')
        close.setCursor(Qt.PointingHandCursor)
        close.setIcon(QIcon(_x_pixmap(11)))
        close.setIconSize(QSize(_s(11), _s(11)))
        close.setFixedSize(_s(24), _s(24))
        close.setToolTip('Dismiss this update notice')
        close.clicked.connect(self._on_update_banner_dismiss)
        self._ub_close = close
        h.addWidget(spark, 0, Qt.AlignVCenter)
        h.addWidget(badge, 0, Qt.AlignVCenter)
        h.addWidget(lbl, 0, Qt.AlignVCenter)
        h.addStretch(1)
        h.addWidget(whatsnew, 0, Qt.AlignVCenter)
        h.addSpacing(_s(8))
        h.addWidget(link, 0, Qt.AlignVCenter)
        h.addWidget(later, 0, Qt.AlignVCenter)
        h.addWidget(close, 0, Qt.AlignVCenter)
        self._ub_label = lbl
        self._ub_whatsnew = whatsnew
        self._ub_btn = link
        self._update_info = None
        self._update_ready_path = ''
        return w

    def _kick_update_check(self):
        """Spawn the daemon-thread poll. Callback runs on the worker thread;
        marshal back to the GUI thread via the queued signal. ignore_cooldown so
        every launch + the periodic re-check reliably surfaces an update (still
        respects a dismissed version, so no nagging)."""
        def _on_avail(info):
            self.update_available_sig.emit(info)
            return None

        try:
            _updater.check_async(_on_avail, ignore_cooldown=True)
        except Exception:
            pass

    def _show_update_banner(self, info) -> None:
        self._update_info = info
        try:
            self._ub_badge.setText(getattr(info, 'version', '') or 'UPDATE')
        except Exception:
            pass
        headline = getattr(info, 'headline', '') or f'Segue {info.version}'
        if _updater.installer_is_directly_updatable(info):
            cta = getattr(info, 'cta_label', '') or 'Update'
        else:
            cta = (getattr(info, 'cta_label', '') or 'Get it') + ' →'
        self._update_ready_path = ''
        self._ub_later.setVisible(False)
        self._ub_btn.setEnabled(True)
        self._clear_btn_fill()
        try:
            self._menu_restart_apply.setVisible(False)
        except Exception:
            pass
        self._ub_label.setText(headline)
        self._ub_label.setToolTip('')
        self._ub_btn.setText(cta)
        self._ub_whatsnew.setVisible(bool(info.notes))
        self._apply_banner_color(getattr(info, 'color', ''))
        self._update_banner.setVisible(True)
        self._update_banner.adjustSize()
        self.adjustSize()
        return None

    def _show_whats_new(self, info=None, post_update: bool = False) -> None:
        """Dialog listing a release's patch notes.

        Pre-update (banner click): `info` defaults to the fetched manifest, and
        the CTA is the Get-it button. Post-update (`post_update=True`, called on
        first launch after updating): `info` is the bundled whatsnew.json and the
        CTA becomes a plain "Got it" that just closes (the user already has it)."""
        if info is None:
            info = getattr(self, '_update_info', None)
        if info is None or not info.notes:
            return None
        dlg = QDialog(self)
        dlg.setWindowTitle("What's new")
        if os.path.exists(_APP_ICON):
            dlg.setWindowIcon(QIcon(_APP_ICON))
        dlg.setStyleSheet(_build_qss(_CHECK))
        dlg.setFont(_ui_font(14))
        dlg.setFixedWidth(_s(470))
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        _hstops = [c.strip() for c in (getattr(info, 'color', '') or _ACCENT).split(',') if c.strip()] or [_ACCENT]
        if len(_hstops) > 1:
            _hpts = ', '.join(f'stop:{i / (len(_hstops) - 1):.3f} {c}' for i, c in enumerate(_hstops))
            _hbg = f'qlineargradient(x1:0, y1:0, x2:1, y2:0, {_hpts})'
            _hrgb = [tuple(int(c.lstrip('#')[k:k + 2], 16) for k in (0, 2, 4)) for c in _hstops]
            _htxt = _contrast_text('#%02x%02x%02x' % tuple(sum(ch) // len(_hrgb) for ch in zip(*_hrgb)))
        else:
            _hbg, _htxt = _hstops[0], _contrast_text(_hstops[0])
        _htxt = '#ffffff'
        _wn_title = QLabel(f"What's new in Segue {info.version}")
        _wn_title.setFont(_ui_font(18, QFont.Bold))
        _wn_title.setAttribute(Qt.WA_StyledBackground, True)
        _wn_title.setStyleSheet(f'background:{_hbg}; color:{_htxt}; padding:{_s(11)}px {_s(22)}px;')
        outer.addWidget(_wn_title)
        scroll = _FadeScroll(fade_bot=64)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(_s(22), _s(16), _s(16), 0)
        v.setSpacing(0)
        _hero_w = _s(470) - 16 - _s(22) - _s(16)
        for _iu in (getattr(info, 'image_url', '') or '').split(','):
            _iu = _iu.strip()
            if not _iu:
                continue
            if _iu.startswith('asset:'):
                _iu = os.path.join(_ASSETS, _iu[len('asset:'):])
            pm = _fetch_pixmap(_iu, _hero_w)
            if pm is None:
                continue
            pic = QLabel()
            pic.setPixmap(_rounded_bordered(pm, _s(10)))
            pic.setAlignment(Qt.AlignLeft)
            v.addWidget(pic)
            v.addSpacing(_s(14))
        import re as _re
        _pc = (getattr(info, 'color', '') or _ACCENT).split(',')[0].strip() or _ACCENT
        _BADGE = {
            'new': (_pc, _contrast_text(_pc), 'NEW'),
            'fix': (_c('surface_hi'), _c('text_dim'), 'FIXED'),
            'improved': (_c('emph_fill'), _c('emph_text'), 'IMPROVED'),
        }

        def _text_label(_html_txt):
            _html = _theme_note_html(_localize_inline_images(_html_txt.replace('\n', '<br>')))
            _b = QLabel(f"<div style='line-height:138%'>{_html}</div>")
            _b.setTextFormat(Qt.RichText)
            _b.setOpenExternalLinks(True)
            _b.setWordWrap(True)
            _b.setFont(_ui_font(14))
            _b.setObjectName('hint')
            _b.setStyleSheet(f'color:{_c("text_dim")};')
            return _b

        _lines = [_l.strip() for _l in info.notes.split('\n') if _l.strip()]
        for _idx, _line in enumerate(_lines):
            _next_img = _idx + 1 < len(_lines) and _lines[_idx + 1].startswith('[[img:')
            _gap = _s(4) if _next_img else _s(15)
            _mi = _re.match('\\[\\[img:(.*?)\\]\\]$', _line)
            if _mi:
                _src = _mi.group(1).strip()
                if _src.startswith('asset:'):
                    _src = os.path.join(_ASSETS, _src[len('asset:'):])
                _pm = _fetch_pixmap(_src, _s(400))
                if _pm is not None:
                    _pic = QLabel()
                    _pic.setPixmap(_rounded_bordered(_pm, _s(10)))
                    _pic.setAlignment(Qt.AlignLeft)
                    v.addWidget(_pic)
                    v.addSpacing(_s(26))
                continue
            _mh = _re.match('\\[\\[(title|sub):(.*?)\\]\\]$', _line)
            if _mh:
                _is_title = _mh.group(1) == 'title'
                if _idx > 0:
                    v.addSpacing(_s(18))
                _hl = QLabel(_mh.group(2).strip())
                _hl.setFont(_ui_font(19 if _is_title else 16, QFont.Bold))
                _hl.setStyleSheet(f'color:{_c("text") if _is_title else _c("text_hint")};')
                v.addWidget(_hl)
                v.addSpacing(_s(9))
                _rule = QFrame()
                _rule.setFixedHeight(max(1, _s(2)))
                _rule.setStyleSheet(f'background:{_pc if _is_title else _c("border")}; border:none;')
                v.addWidget(_rule)
                v.addSpacing(_s(12))
                continue
            _mb = _re.match('\\[\\[(new|fix|improved)\\]\\]\\s*(.*)$', _line)
            if _mb:
                from PySide6.QtWidgets import QGridLayout
                _bg, _fg, _txt = _BADGE[_mb.group(1)]
                _bd = QLabel(_txt)
                _bf = _ui_font(10, QFont.Black)
                try:
                    _bf.setLetterSpacing(QFont.PercentageSpacing, 103)
                except Exception:
                    pass
                _bd.setFont(_bf)
                _bd.setStyleSheet(f'background:{_bg}; color:{_fg}; border-radius:{_s(5)}px; padding:{_s(2)}px {_s(8)}px; margin-top:{_s(2)}px;')
                _bd.setAlignment(Qt.AlignCenter)
                _bd.adjustSize()
                _indent = _bd.sizeHint().width() + _s(9)
                _inner = _theme_note_html(_localize_inline_images(_mb.group(2).replace('\n', '<br>')))
                _body = QLabel(f"<div style='line-height:138%; text-indent:{_indent}px'>{_inner}</div>")
                _body.setTextFormat(Qt.RichText)
                _body.setOpenExternalLinks(True)
                _body.setWordWrap(True)
                _body.setFont(_ui_font(14))
                _body.setObjectName('hint')
                _body.setStyleSheet(f'color:{_c("text_dim")};')
                _cont = QWidget()
                _grid = QGridLayout(_cont)
                _grid.setContentsMargins(0, 0, 0, 0)
                _grid.setSpacing(0)
                _grid.addWidget(_body, 0, 0)
                _grid.addWidget(_bd, 0, 0, Qt.AlignTop | Qt.AlignLeft)
                v.addWidget(_cont)
                v.addSpacing(_gap)
                continue
            v.addWidget(_text_label(_line))
            v.addSpacing(_gap)
        v.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        row = QHBoxLayout()
        if getattr(info, 'changelog_url', ''):
            cl = _HoverLink('Full changelog', info.changelog_url)
            row.addWidget(cl)
        row.addStretch(1)
        get = QPushButton('Got it' if post_update else (getattr(info, 'cta_label', '') or 'Get it') + ' →')
        get.setObjectName('savebtn')
        get.setCursor(Qt.PointingHandCursor)
        if _active_theme() == 'light':
            get.setStyleSheet(f'QPushButton#savebtn {{ background:{_hbg}; color:{_htxt}; border:none; border-radius:{_s(8)}px; padding:{_s(9)}px {_s(12)}px; font-weight:700; }}QPushButton#savebtn:hover {{ background:{_hbg}; }}')
        if post_update:
            get.clicked.connect(dlg.accept)
        else:
            get.clicked.connect(lambda: (self._on_update_banner_click(), dlg.accept()))
        row.addWidget(get)
        row.setContentsMargins(_s(22), _s(12), _s(22), _s(18))
        _btnwrap = QWidget()
        _btnwrap.setLayout(row)
        outer.addWidget(_btnwrap)
        try:
            cap = int(QGuiApplication.primaryScreen().availableGeometry().height() * 0.85)
        except Exception:
            cap = _s(700)
        dlg.setMaximumHeight(cap)
        dlg.resize(_s(470), min(dlg.sizeHint().height(), cap))
        dlg.exec()
        return None

    def show_post_update_whatsnew(self) -> None:
        """First launch after updating: open this version's What's-new from the
        BUNDLED whatsnew.json (asset: image refs, works offline) in post-update
        mode (CTA = "Got it", just closes). Caller gates on the version bump."""
        import json
        from fh6_spotify.updater import UpdateInfo
        try:
            with open(os.path.join(_ASSETS, 'whatsnew.json'), encoding='utf-8') as f:
                d = json.load(f)
            info = UpdateInfo(
                version=d.get('version', ''),
                ko_fi_url=d.get('ko_fi_url', ''),
                notes=d.get('notes', ''),
                installer_url=d.get('installer_url', ''),
                headline=d.get('headline', ''),
                cta_label=d.get('cta_label', ''),
                changelog_url=d.get('changelog_url', ''),
                color=d.get('color', ''),
                image_url=d.get('image_url', ''),
            )
            if not info.notes:
                return None
            self._show_whats_new(info=info, post_update=True)
            return None
        except Exception:
            return None

    def _apply_banner_color(self, color: str) -> None:
        """Per-release banner color (from latest.json `color`). Empty -> revert
        to the default accent (global QSS). Accepts a single hex OR comma-
        separated hexes for a left->right gradient ("#FF2E97,#FF8A2E").
        Text + button colors are derived for contrast so any background stays
        readable (gradient contrast uses the average of the stops)."""
        if not color:
            color = _ACCENT
        stops = [c.strip() for c in color.split(',') if c.strip()]
        if len(stops) > 1:
            pts = ', '.join((f'stop:{i / (len(stops) - 1):.3f} {c}' for i, c in enumerate(stops)))
            bg = f'qlineargradient(x1:0, y1:0, x2:1, y2:0, {pts})'
            rgbs = [tuple((int(c.lstrip('#')[k:k + 2], 16) for k in (0, 2, 4))) for c in stops]
            avg = '#%02x%02x%02x' % tuple((sum(ch) // len(rgbs) for ch in zip(*rgbs)))
            txt = _contrast_text(avg)
            btn_col = avg
        else:
            bg = stops[0]
            txt = _contrast_text(stops[0])
            btn_col = stops[0]
        txt = '#ffffff'
        if getattr(self, '_ub_close', None) is not None:
            self._ub_close.setIcon(QIcon(_x_pixmap(11, txt)))
        if getattr(self, '_ub_spark', None) is not None:
            self._ub_spark.setPixmap(_update_pixmap(14, txt))
        if getattr(self, '_ub_label', None) is not None:
            self._ub_label.setTextColor(txt)
        self._ub_btn_bg = txt
        self._ub_btn_fg = btn_col
        self._update_banner.setStyleSheet(f"QWidget#updatebanner {{ background: {bg}; }}QLabel#updatebannertext {{ color: {txt}; padding-left: {_s(2)}px; }}QPushButton#updatebannerbtn {{ background: {txt}; color: {btn_col}; border:none; border-radius:{_s(6)}px; padding:{_s(4)}px {_s(12)}px; min-height:0; }}QPushButton#updatebannerlink {{ background:transparent; color:{txt}; border:none; padding:0 {_s(6)}px; font-weight:600; text-decoration:underline; }}QPushButton#updatebannerclose {{ background:transparent; color:{txt}; border:none; padding:0; }}QPushButton#updatebannerclose:hover {{ background: rgba(0,0,0,0.12); border-radius:{_s(12)}px; }}")
        return None

    def _on_update_banner_click(self) -> None:
        if getattr(self, '_update_ready_path', ''):
            _updater.apply_update(self._update_ready_path, self._quit_fn)
            return None
        info = self._update_info
        if info is not None and _updater.installer_is_directly_updatable(info):
            self._start_in_app_update(info)
            return None
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        url = (getattr(info, 'installer_url', '') if info else '') or (info.ko_fi_url if info else '')
        if url:
            QDesktopServices.openUrl(QUrl(url))
            return None

    def _start_in_app_update(self, info) -> None:
        self._ub_btn.setEnabled(False)
        self._ub_btn.setText('Downloading 0%')
        self._set_btn_fill(0)
        self._ub_whatsnew.setVisible(False)

        def _work():
            try:
                def _prog(done, total):
                    self.update_progress_sig.emit(int(done), int(total))
                    return None

                path = _updater.download_installer(info, on_progress=_prog)
                self.update_ready_sig.emit(path)
                return None
            except Exception as exc:
                self.update_failed_sig.emit(str(exc))
                return None

        import threading
        threading.Thread(target=_work, name='segue-update-dl', daemon=True).start()
        return None

    def _on_update_progress(self, done: int, total: int) -> None:
        pct = int(done * 100 / total) if total > 0 else 0
        self._ub_btn.setText('Downloading {}%'.format(pct) if total > 0 else 'Downloading…')
        self._set_btn_fill(pct if total > 0 else 0)
        return None

    def _set_btn_fill(self, pct: int) -> None:
        """Progress fill that adapts to the CURRENT button bg/fg (defaults match
        the QSS dark default; custom per-release/light-mode banner colors come
        from _apply_banner_color via _ub_btn_bg/_ub_btn_fg). The fill is the
        button bg mixed ~14% toward its contrast text so it reads as a subtle
        lighter/darker shade that always fits."""
        bg = getattr(self, '_ub_btn_bg', None) or '#1f1f1e'
        fg = getattr(self, '_ub_btn_fg', None) or _ACCENT

        def _hex(s):
            s = (s or '').lstrip('#')
            if len(s) == 3:
                s = ''.join(ch * 2 for ch in s)
            return tuple(int(s[k:k + 2], 16) for k in (0, 2, 4))

        def _mix(a, b, t):
            return '#%02x%02x%02x' % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

        try:
            br, bgc = _hex(bg), _hex('#ffffff')
            if (br[0] + br[1] + br[2]) / 3 > 160:
                bgc = _hex('#000000')
            fill = _mix(br, bgc, 0.14)
        except Exception:
            fill = '#2b2b29'
        p = max(0.0, min(1.0, pct / 100.0))
        e = min(p + 0.0008, 1.0)
        r = _s(6)
        pv = _s(4)
        ph = _s(12)
        self._ub_btn.setStyleSheet(
            'QPushButton#updatebannerbtn{{border:none; border-radius:{r}px; padding:{pv}px {ph}px; min-height:0; color:{c}; background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {f}, stop:{p:.4f} {f},stop:{e:.4f} {b}, stop:1 {b});}}'.format(
                r=r, pv=pv, ph=ph, c=fg, f=fill, b=bg, p=p, e=e
            )
        )
        return None

    def _clear_btn_fill(self) -> None:
        self._ub_btn.setStyleSheet('')
        return None

    def _on_update_ready(self, path: str) -> None:
        self._update_ready_path = path
        self._clear_btn_fill()
        self._ub_label.setText('Update ready - restart Segue to apply')
        self._ub_btn.setEnabled(True)
        self._ub_btn.setText('Restart now')
        self._ub_later.setVisible(True)
        return None

    def _on_update_failed(self, msg: str) -> None:
        self._update_ready_path = ''
        self._clear_btn_fill()
        self._ub_label.setText('Update failed - download manually')
        self._ub_btn.setEnabled(True)
        info = self._update_info
        self._ub_btn.setText((getattr(info, 'cta_label', '') or 'Get it') + ' →' if info else 'Get it →')
        return None

    def _on_update_later(self) -> None:
        self._update_banner.setVisible(False)
        try:
            self._menu_restart_apply.setVisible(bool(self._update_ready_path))
        except Exception:
            pass

    def _on_menu_restart_apply(self) -> None:
        """Hamburger-menu shortcut: apply the cached/verified installer + relaunch.
        Mirrors the banner's Restart now path; safe no-op if nothing is ready."""
        path = getattr(self, '_update_ready_path', '')
        if path:
            _updater.apply_update(path, self._quit_fn)
            return None

    def _on_update_banner_dismiss(self) -> None:
        info = getattr(self, '_update_info', None)
        if info:
            try:
                _updater.dismiss_version(info.version)
            except Exception:
                pass
        self._update_banner.setVisible(False)
        if getattr(self, '_update_status', None) is not None:
            self._update_status.setText('')
        self.adjustSize()
        return None

    def _preview_update_banner(self) -> None:
        """Demo-only: show a sample banner so headline + What's new can be
        previewed without a real release."""
        from fh6_spotify.updater import UpdateInfo
        _staged = os.path.join(_ASSETS, 'whatsnew.json')
        if os.path.exists(_staged):
            try:
                import json as _json
                with open(_staged, encoding='utf-8') as f:
                    d = _json.load(f)
                self._show_update_banner(UpdateInfo(version=d.get('version', ''), ko_fi_url=d.get('ko_fi_url', ''), notes=d.get('notes', ''), installer_url=d.get('installer_url', ''), headline=d.get('headline', ''), cta_label=d.get('cta_label', ''), changelog_url=d.get('changelog_url', ''), color=d.get('color', ''), image_url=d.get('image_url', '')))
                return None
            except Exception:
                pass
        _hero = os.path.normpath(os.path.join(_ASSETS, '..', '..', 'docs', 'release-assets', 'whatsnew_121_forza.png'))
        _duck_img = os.path.normpath(os.path.join(_ASSETS, '..', '..', 'docs', 'release-assets', 'discord_ducking.png')).replace('\\', '/')
        _garage_img = os.path.normpath(os.path.join(_ASSETS, '..', '..', 'docs', 'release-assets', 'garage_skipping.jpg')).replace('\\', '/')
        self._show_update_banner(UpdateInfo(version='1.3.0', ko_fi_url='https://ko-fi.com/segueapp', notes=f"🏁 <b>Backwards compatible with older Forzas.</b> The Forza preset now covers Horizon 4, 5 and 6: auto-detect, menus, garage and ducking all work whichever one you launch.<br><br><span style='color:#FFC53D'>⚠️ You'll need telemetry on in those games too: same Data Out steps as the setup guide.</span><br><br>🚗 <b>Skipping finally behaves everywhere.</b> D-pad skip locks itself through every menu, garage, Auto Show and car picker (volume dips too), and snaps back the moment you drive off. Parked in the open world still skips fine.<br><img src='{_garage_img}' width='380'><br>🎙️ <b>Discord ducking.</b> Turn on \"Include Discord\" and your music ducks when friends talk, in Forza or any game. The new \"Include self\" option ducks when YOU talk back too.<br><img src='{_duck_img}' width='380'><br>⚡ <b>Massive CPU drop.</b> Speech detection now uses about 60x less CPU, and it pauses itself in menus.<br><br>🏎️ <b>Sim wheel fix.</b> Segue no longer breaks force feedback, wheel input is now fully read-only.<br><br>🥅 <b>Rocket League:</b> music ducks under Discord voice, and Left stick press is the new skip option.<br><br>🎮 <b>Summon Segue from your controller.</b> Hold the mic button (rebindable on every pad) and the app pops up right over your game, hold again to hide it. Keyboard works everywhere too: Ctrl+Shift+Alt+S.<br><br>📏 <b>Overlay glow-up.</b> Drag it to ANY monitor, resize up to 200% with a corner drag or scroll, and hold Shift while dragging for clean alignment guides that snap to the edges and center.<br><br>🎵 <b>TIDAL and Amazon Music</b> added as sources, tucked into the source menu's new \"More\" page.<br><br>🛠️ Plus: touchpad tap sensitivity slider, DualShock 4 Bluetooth fix, non-DualSense pads now work in game, controller auto-reconnect, dark tooltips, overlay stays above the game, decluttered Controls, and a permanent Discord invite.", headline='Segue 1.3 is out 🏁 Garage-proof skipping, Discord ducking + more', cta_label='Get 1.3.0', color='#0E8C6B', image_url=_hero))
        return None

    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('titlebar')
        bar.setFixedHeight(self._titlebar_h)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self._menu_btn = QPushButton()
        self._menu_btn.setObjectName('menubtn')
        self._menu_btn.setIcon(_menu_icon())
        self._menu_btn.setIconSize(QSize(_s(13), _s(13)))
        self._menu_btn.setFixedSize(_s(46), self._titlebar_h)
        self._menu_btn.setCursor(Qt.PointingHandCursor)
        self._build_menu()
        from PySide6.QtGui import QCursor
        self._menu_btn.clicked.connect(lambda: self._menu.popup(QCursor.pos()))
        self._menu.aboutToHide.connect(lambda: (self._menu_btn.setAttribute(Qt.WA_UnderMouse, False), self._menu_btn.update()))
        h.addWidget(self._menu_btn)
        self._verbtn = _VerButton(_APP_VERSION_LABEL or f'v{_APP_VERSION} early access')
        self._verbtn.setFont(_ui_font(13))
        self._verbtn.setIconSize(QSize(_s(15), _s(15)))
        self._verbtn.setToolTip('Check for updates')
        self._verbtn.clicked.connect(self._on_update_click)
        h.addSpacing(_s(6))
        h.addWidget(self._verbtn, 0, Qt.AlignVCenter)
        self._update_status = QLabel('')
        self._update_status.setFont(_ui_font(12))
        self._update_status.setStyleSheet(f"color:{_c('success')};")
        self._update_status.setContentsMargins(_s(8), 0, 0, 0)
        h.addWidget(self._update_status, 0, Qt.AlignVCenter)
        h.addStretch(1)
        self._btn_min = self._cap_btn('min', self.showMinimized)
        self._btn_max = self._cap_btn('max', self._toggle_max)
        self._btn_close = self._cap_btn('close', self.close, close=True)
        h.addWidget(self._btn_min)
        h.addWidget(self._btn_max)
        h.addWidget(self._btn_close)
        return bar

    def _cap_btn(self, kind: str, slot, close: bool = False) -> QPushButton:
        b = QPushButton()
        b.setObjectName('capclose' if close else 'capbtn')
        if close:
            b._ico_rest = _caption_icon(kind)
            b._ico_hot = _caption_icon(kind, color='#ffffff')
            b.setIcon(b._ico_rest)
            b.enterEvent = lambda e, btn=b: (QPushButton.enterEvent(btn, e), btn.setIcon(btn._ico_hot))
            b.leaveEvent = lambda e, btn=b: (QPushButton.leaveEvent(btn, e), btn.setIcon(btn._ico_rest))
        else:
            b.setIcon(_caption_icon(kind))
        b.setIconSize(QSize(_s(18), _s(18)))
        b.setFixedSize(_s(46), self._titlebar_h)
        b.clicked.connect(slot)
        return b

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._btn_max.setIcon(_caption_icon('restore' if self.isMaximized() else 'max'))
        return None

    def _win_is_maximized(self) -> bool:
        hwnd = getattr(self, '_hwnd', 0)
        if not hwnd:
            return self.isMaximized()
        try:
            wp = _WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
            ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            return wp.showCmd == 3
        except Exception:
            return self.isMaximized()

    def nativeEvent(self, eventType, message):
        if eventType != b'windows_generic_MSG' or sys.platform != 'win32':
            return (False, 0)
        msg = wintypes.MSG.from_address(int(message))
        tb = getattr(self, '_taskbar', None)
        if tb is not None:
            if tb.msg_created and msg.message == tb.msg_created:
                tb.add('msg')
                return (False, 0)
            if msg.message == 273 and tb.handle_command(msg.wParam):
                return (True, 0)
        if msg.message == 131:
            if msg.wParam:
                if self._win_is_maximized():
                    g = ctypes.windll.user32.GetSystemMetrics
                    cx = g(32) + g(92)
                    cy = g(33) + g(92)
                    r = _NCCALCSIZE_PARAMS.from_address(msg.lParam).rgrc[0]
                    r.left += cx
                    r.right -= cx
                    r.top += cy
                    r.bottom -= cy
                return (True, 0)
            return (False, 0)
        if msg.message == 132:
            return (True, self._hit_test(msg.lParam))
        if msg.message == 161 and msg.wParam == 2:
            self._close_open_pickers()
        return (False, 0)

    def _hit_test(self, lparam: int) -> int:
        x = lparam & 65535
        y = (lparam >> 16) & 65535
        if x > 32767:
            x -= 65536
        if y > 32767:
            y -= 65536
        hwnd = getattr(self, '_hwnd', 0)
        if not hwnd:
            return 1
        rect = _RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pw, ph = rect.right - rect.left, rect.bottom - rect.top
        if pw <= 0 or ph <= 0:
            return 1
        lx, ly = x - rect.left, y - rect.top
        scale = pw / self.width() if self.width() else 1.0
        b = max(6, int(round(8 * scale)))
        code = 1
        if not self.isMaximized():
            left, right = lx < b, lx > pw - b
            top, bottom = ly < b, ly > ph - b
            if top and left:
                code = 13
            elif top and right:
                code = 14
            elif bottom and left:
                code = 16
            elif bottom and right:
                code = 17
            elif left:
                code = 10
            elif right:
                code = 11
            elif top:
                code = 12
            elif bottom:
                code = 15
        if code == 1 and ly <= self._titlebar_h * scale:
            child = self.childAt(QPoint(int(lx / scale), int(ly / scale)))
            code = 1 if isinstance(child, QAbstractButton) else 2
        return code

    def _kill_ui_focus(self):
        """Make no widget keyboard-focusable so arrow keys can't navigate or
        nudge sliders. Mouse interaction (clicks, slider drag) is unaffected."""
        for w in self.findChildren(QWidget):
            w.setFocusPolicy(Qt.NoFocus)
        self.setFocusPolicy(Qt.NoFocus)
        self.clearFocus()
        return None

    def _card_for(self, name: str) -> '_Card':
        if name == 'mixer':
            return self._mixer_card
        return self._adv_card

    def _set_tab(self, name: str):
        """Accordion: one panel open at a time. Clicking the open tab collapses it
        (hides the faders). Instant - no animation."""
        new = None if self._active_tab == name else name
        if new == self._active_tab:
            return None
        self._active_tab = new
        self._style_tabs()
        for nm in ('mixer', 'extras'):
            c = self._card_for(nm)
            open_ = nm == new
            c.setVisible(open_)
            c.setMinimumHeight(0)
            c.setMaximumHeight(16777215 if open_ else 0)
        QTimer.singleShot(0, self._fit_window)
        return None

    def _fit_window(self):
        try:
            scr_h = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry().height()
        except Exception:
            scr_h = 1000000
        if self.isMaximized() or self.isFullScreen():
            self.update()
            return None

        def _refresh(lay):
            if lay is None:
                return None
            lay.invalidate()
            for i in range(lay.count()):
                it = lay.itemAt(i)
                w = it.widget()
                if w is not None:
                    w.updateGeometry()
                    if w.layout() is None:
                        continue
                    _refresh(w.layout())
                    continue
                if it.layout() is not None:
                    _refresh(it.layout())
            return None

        _refresh(self.layout())
        self.layout().activate()
        h = self.layout().sizeHint().height()
        if h > 0:
            h = min(h, scr_h - _s(24))
            if self.height() >= scr_h - 2 and h >= scr_h - _s(24):
                self.update()
                return None
            self.setMaximumHeight(h)
            self.resize(self.width(), h)
            QTimer.singleShot(0, lambda: self.setMaximumHeight(16777215))
            try:
                avail = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
                if self.y() + h > avail.bottom() - _s(8):
                    self.move(self.x(), max(avail.top() + _s(8), avail.bottom() - h - _s(8)))
            except Exception:
                pass
        self.update()
        return None

    def _update_tabs(self):
        """Initial state: reflect _active_tab on the buttons + show its panel."""
        self._style_tabs()
        for name in ('mixer', 'extras'):
            c = self._card_for(name)
            open_ = self._active_tab == name
            c.setVisible(open_)
            c.setMinimumHeight(0)
            c.setMaximumHeight(16777215 if open_ else 0)
        return None

    def _style_tabs(self):
        """Active tab: bright + grown to meet its panel. Sibling (when the other is
        open): dimmed. Neither open: both normal."""
        a = self._active_tab
        for name, btn, ic, icd in (('mixer', self._tab_mixer, self._ic_mixer, self._ic_mixer_dull), ('extras', self._tab_extras, self._ic_extras, self._ic_extras_dull)):
            active = a == name
            dull = (a is not None) and (not active)
            self._set_prop(btn, 'active', active)
            self._set_prop(btn, 'dull', dull)
            btn.setIcon(icd if dull else ic)
        return None

    def _build_menu(self):
        self._menu = _FadeMenu(self)
        self._menu.setFont(_ui_font(14))
        self._menu.setCursor(Qt.PointingHandCursor)

        self._presets_menu = _FadeMenu('Presets', self._menu)
        _round_menu(self._presets_menu)
        self._presets_menu.setFont(_ui_font(14))
        self._presets_menu.setCursor(Qt.PointingHandCursor)
        self._presets_menu.aboutToShow.connect(self._rebuild_presets_menu)
        self._menu.addMenu(self._presets_menu)

        self._ovl_pre_menu = _FadeMenu('Overlay presets', self._menu)
        _round_menu(self._ovl_pre_menu)
        self._ovl_pre_menu.setFont(_ui_font(14))
        self._ovl_pre_menu.setCursor(Qt.PointingHandCursor)
        self._ovl_pre_menu.aboutToShow.connect(self._rebuild_ovl_presets)

        from PySide6.QtGui import QActionGroup
        scale_menu = _FadeMenu('View', self._menu)
        _round_menu(scale_menu)
        scale_menu.setFont(_ui_font(14))
        scale_menu.setCursor(Qt.PointingHandCursor)
        grp = QActionGroup(self)
        grp.setExclusive(True)
        for step in _SCALE_STEPS:
            a = scale_menu.addAction(_scale_label(step))
            a.setCheckable(True)
            a.setChecked(abs(step - self._cfg.ui_scale) < 1e-06)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, st=step: self._set_scale(st))
        self._menu.addMenu(scale_menu)

        help_menu = _FadeMenu('Help', self._menu)
        _round_menu(help_menu)
        help_menu.setFont(_ui_font(14))
        help_menu.setCursor(Qt.PointingHandCursor)
        _isz = _s(20)
        self._help_icon_style = _BigMenuIconStyle(_isz)
        help_menu.setStyle(self._help_icon_style)
        _hg = help_menu.addAction('Open guide')
        _hg.triggered.connect(self._open_help)
        _hg.setIcon(QIcon(_question_pixmap(20, _c('icon_dim'))))
        _hr = help_menu.addAction('Replay tour')
        _hr.triggered.connect(self._replay_intro)
        _hr.setIcon(QIcon(_play_pixmap(20, _c('icon_dim'))))
        _hu = help_menu.addAction('Check for updates')
        _hu.triggered.connect(self._on_update_click)
        _hu.setIcon(QIcon(_refresh_pixmap(20, _c('icon_dim'), 0.0)))
        _hra = help_menu.addAction('Restart to apply update')
        _hra.triggered.connect(self._on_menu_restart_apply)
        _hra.setIcon(QIcon(_refresh_pixmap(20, _c('icon_dim'), 0.0)))
        _hra.setVisible(False)
        self._menu_restart_apply = _hra
        _hd = help_menu.addAction('Join our Discord')
        _hd.triggered.connect(self._open_discord)
        _disc_pm = _tinted(os.path.join(_ASSETS, 'Discord_logo_blue.png'), _c('icon_dim'), 20)
        if _disc_pm:
            _hd.setIcon(QIcon(_disc_pm))
        self._menu.addMenu(help_menu)

        _gear_pm = _tinted(os.path.join(_ASSETS, 'settings.png'), _c('icon_dim'), 15)
        self._icon_right_row(self._menu, 'Preferences…', _gear_pm, self._open_preferences)

        if _is_dev():
            self._menu.addAction('Stream overlay…').triggered.connect(self._open_overlay_editor)
            self._act_demo = self._menu.addAction('Demo mode    ✓' if self._cfg.demo_mode else 'Demo mode')
            self._act_demo.triggered.connect(self._toggle_demo_mode)
            self._act_preview = self._menu.addAction('Preview update banner')
            self._act_preview.triggered.connect(self._preview_update_banner)

        self._menu.addSeparator()

        _un_pm = _tinted(os.path.join(_ASSETS, 'Uninstall icon v2 red.png'), _c('icon_dim'), 14)
        self._icon_right_row(self._menu, 'Uninstall…', _un_pm, self._menu_uninstall)

        _q_pm = _tinted(os.path.join(_ASSETS, 'on-off-button for hamburger menu.png'), _c('icon_dim'), 14)
        self._icon_right_row(self._menu, 'Quit', _q_pm, self._menu_quit)

        _old_ht = getattr(self, '_menu_hover_timer', None)
        if _old_ht is not None:
            _old_ht.stop()
            _old_ht.deleteLater()
        self._menu_hover_timer = QTimer(self)
        self._menu_hover_timer.setInterval(180)
        self._menu_outside_ticks = 0
        self._menu_hover_timer.timeout.connect(self._menu_hover_check)
        self._menu.aboutToShow.connect(lambda: (setattr(self, '_menu_outside_ticks', 0), self._menu_hover_timer.start()))
        self._menu.aboutToHide.connect(self._menu_hover_timer.stop)
        return None

    def _icon_right_row(self, menu, text, icon_pm, handler):
        """Menu row with the label left + a small icon pinned RIGHT (Qt only
        draws left icons natively). QWidgetAction so we control the layout;
        replicates the hover highlight + click-to-trigger. Keyboard nav is lost
        for these rows, which is fine for a mouse-driven hamburger menu."""
        from PySide6.QtWidgets import QWidgetAction, QWidget, QHBoxLayout, QLabel
        act = QWidgetAction(menu)
        row = QWidget()
        row.setObjectName('menurowR')
        row.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(_s(10), _s(7), _s(12), _s(7))
        lay.setSpacing(_s(10))
        t = QLabel(text)
        t.setObjectName('menurowtext')
        t.setFont(_ui_font(14))
        lay.addWidget(t)
        lay.addStretch(1)
        ic = QLabel()
        ic.setStyleSheet('background: transparent;')
        if icon_pm is not None and not icon_pm.isNull():
            ic.setPixmap(icon_pm)
        lay.addWidget(ic, 0, Qt.AlignVCenter)
        act.setDefaultWidget(row)

        def _release(_e):
            menu.close()
            handler()
            return None

        row.mouseReleaseEvent = _release
        menu.addAction(act)
        return act

    def _menu_hover_check(self):
        """Close the hamburger menu when the cursor leaves it + ANY open
        submenu by a margin for two consecutive ticks. 'Inside' counts the
        widget under the cursor being any QMenu (covers App scale / Presets
        fly-outs, whose geometry we don't track explicitly) plus a margin
        around the main menu rect as grace when sliding between them."""
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QApplication, QMenu
        if not self._menu.isVisible():
            self._menu_hover_timer.stop()
            return None
        pos = QCursor.pos()
        margin = _s(36)
        inside = False
        w = QApplication.widgetAt(pos)
        while w is not None:
            if isinstance(w, QMenu):
                inside = True
                break
            w = w.parent() if hasattr(w, 'parent') else None
        if not inside:
            for mw in (self._menu, getattr(self, '_presets_menu', None)):
                if mw is None:
                    continue
                if not mw.isVisible():
                    continue
                g = mw.geometry().adjusted(-margin, -margin, margin, margin)
                if g.contains(pos):
                    inside = True
                    break
        if inside:
            self._menu_outside_ticks = 0
            return None
        self._menu_outside_ticks += 1
        if self._menu_outside_ticks >= 2:
            self._menu.close()
            return None

    def _place_viz_badge(self):
        """Pin the 'New' badge over the visualizer button's top-right
        corner, poking past the edges."""
        badge = getattr(self, '_viz_badge', None)
        btn = getattr(self, '_viz_btn', None)
        if badge is None or btn is None or not btn.isVisible():
            if badge is not None:
                badge.hide()
            return None
        from PySide6.QtCore import QPoint
        tr = btn.mapTo(self, QPoint(btn.width(), 0))
        badge.adjustSize()
        badge.move(tr.x() - badge.width() + _s(9), tr.y() - badge.height() // 2 + _s(1))
        badge.show()
        badge.raise_()
        return None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_viz_badge()
        return None

    def _open_visualizer(self):
        """View -> Visualizer (beta): fullscreen spectrum on the second
        monitor (primary when there's only one). ESC / double-click closes."""
        try:
            from fh6_spotify.visualizer import VisualizerWindow
            old = getattr(self, '_visualizer', None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self._visualizer = VisualizerWindow(self._cfg, self._ui)
            return None
        except Exception as exc:
            print(f'visualizer failed: {exc}')
            return None

    def _np_hover(self, w, on: bool):
        """Underline the title/artist on hover when it's a live Spotify link."""
        if not getattr(self, '_np_clickable', False):
            return None
        if w is self._np_artist and not self._np_raw_artist:
            return None
        f = w.font()
        f.setUnderline(on)
        w.setFont(f)
        return None

    def _open_spotify_link(self, kind: str):
        """Click the now-playing title/artist -> open the exact Spotify page. kind =
        'album' (the release the track is in) or 'artist'. Spotify source only.
        Resolves name -> ID via the getsegue.app Worker; falls back to a Spotify
        search. Network + shell open run off the GUI thread."""
        if not getattr(self, '_np_clickable', False):
            return None
        track = self._np_raw_title
        artist = self._np_raw_artist
        if kind == 'artist' and not artist:
            return None
        if not track and not artist:
            return None

        def _run():
            try:
                from fh6_spotify import spotify_links as _sl
                r = _sl.resolve(track, artist)
                uri = ''
                if r:
                    uri = (r.get(kind) or {}).get('uri') or ''
                if not uri:
                    uri = _sl.search_uri(artist if kind == 'artist' else f'{track} {artist}')
                os.startfile(uri)
                return None
            except Exception:
                pass

        import threading
        threading.Thread(target=_run, daemon=True).start()
        return None

    def _toggle_demo_mode(self):
        """Hamburger menu -> Demo mode. Flips cfg.demo_mode so the status
        refresher fakes the 'game connected' state. Only affects what the
        Segue window shows - real volume control still requires Spotify."""
        self._cfg.demo_mode = not self._cfg.demo_mode
        self._queue_save()
        self._act_demo.setText('Demo mode    ✓' if self._cfg.demo_mode else 'Demo mode')
        if self._cfg.demo_mode:
            self._link_was_linked = False
        self._refresh_status()
        return None

    def _replay_intro(self):
        """Hamburger menu -> Show intro again. Clears tour_done so the
        welcome dialog + spotlight tour can fire on demand."""
        self._cfg.tour_done = False
        self._tour_started = False
        self._queue_save()
        QTimer.singleShot(120, lambda: self._maybe_run_tour(force=True))
        return None

    def _open_discord(self):
        """Hamburger menu / footer -> open the community Discord invite."""
        try:
            import webbrowser
            webbrowser.open(_DISCORD_URL)
            return None
        except Exception:
            pass

    def _on_update_click(self):
        """Version+refresh unit clicked: spin the icon, run a forced check, then
        either let the orange banner show (update found) or flash a green
        'Up to date' (nothing newer)."""
        self._update_status.setText('')
        if getattr(self, '_spin_timer', None) is None:
            self._spin_angle = 0.0
            self._spin_timer = QTimer(self)
            self._spin_timer.setTimerType(Qt.PreciseTimer)
            self._spin_timer.timeout.connect(self._spin_step)
        self._spin_timer.start(16)
        if self._cfg.demo_mode:
            QTimer.singleShot(900, lambda: (self._preview_update_banner(), self._update_check_settle()))
            return None

        def _on_avail(info):
            self.update_available_sig.emit(info)
            return None

        try:
            _updater.check_async(_on_avail, force=True)
        except Exception:
            pass
        QTimer.singleShot(1600, self._update_check_settle)
        return None

    def _spin_step(self):
        self._spin_angle = (getattr(self, '_spin_angle', 0.0) - 8) % 360
        self._verbtn.set_angle(self._spin_angle)
        return None

    def _update_check_settle(self):
        t = getattr(self, '_spin_timer', None)
        if t is not None:
            t.stop()
        self._spin_angle = 0.0
        self._verbtn.set_angle(0.0)
        if self._update_banner.isVisible():
            self._update_status.setStyleSheet(f'color:{_ACCENT};')
            self._update_status.setText('Update available!')
            self._verbtn.show_refresh()
            return None
        self._update_status.setStyleSheet(f"color:{_c('success')};")
        self._update_status.setText('Up to date')
        self._verbtn.show_check()
        QTimer.singleShot(2600, lambda: (self._update_status.setText(''), self._verbtn.show_refresh()))
        return None

    def _force_update_check(self):
        """Hamburger menu -> Check for updates. Bypasses the 24h cooldown
        and also bypasses the per-version dismiss state so the user can
        re-summon a banner they dismissed earlier."""
        def _on_avail(info):
            self.update_available_sig.emit(info)
            return None

        try:
            _updater.check_async(_on_avail, force=True)
        except Exception:
            pass
        QTimer.singleShot(1500, self._maybe_show_no_update_toast)
        return None

    def _maybe_show_no_update_toast(self):
        if self._update_banner.isVisible():
            return None
        try:
            self.setWindowTitle(f'Segue: up to date (v{_APP_VERSION})')
            QTimer.singleShot(2500, lambda: self.setWindowTitle('Segue'))
        except Exception:
            pass

    def _rebuild_presets_menu(self):
        """(Re)populate the native Presets submenu. 'Save current…' action +
        one QWidgetAction row per preset (load button + trash button). Rebuilt
        on aboutToShow so the list is always fresh after a save/delete."""
        from PySide6.QtWidgets import QWidgetAction
        m = self._presets_menu
        names_now = sorted(_presets.load_presets().keys())
        sig = (tuple(names_now), self._cfg.game_preset)
        if sig == getattr(self, '_presets_menu_sig', None):
            return None
        self._presets_menu_sig = sig
        for act in m.actions():
            w = act.defaultWidget() if hasattr(act, 'defaultWidget') else None
            if w is None:
                continue
            w.deleteLater()
        m.clear()
        self._preset_row_actions = {}

        def _close_all():
            self._presets_menu.close()
            self._menu.close()
            return None

        save_row = QWidget()
        save_row.setObjectName('menurowR')
        save_row.setCursor(Qt.PointingHandCursor)
        sh = QHBoxLayout(save_row)
        sh.setContentsMargins(_s(12), _s(7), _s(12), _s(7))
        sh.setSpacing(_s(8))
        _stext = QLabel('Save current…')
        _stext.setObjectName('menurowtext')
        _stext.setFont(_ui_font(14))
        sh.addWidget(_stext)
        sh.addStretch(1)
        _sicon = QLabel()
        _spm = _tinted(os.path.join(_ASSETS, 'save icon v2.png'), _c('icon_dim'), 15)
        if _spm is not None:
            _sicon.setPixmap(_spm)
        _sicon.setStyleSheet('background: transparent;')
        sh.addWidget(_sicon, 0, Qt.AlignVCenter)

        def _do_save(_e=None):
            self._presets_menu.close()
            self._menu.close()
            QTimer.singleShot(60, self._save_preset)
            return None
        save_row.mouseReleaseEvent = _do_save
        save_wa = QWidgetAction(m)
        save_wa.setDefaultWidget(save_row)
        m.addAction(save_wa)

        all_presets = _presets.load_presets()
        cur_game = self._cfg.game_preset
        from fh6_spotify import game_presets as _gp

        def _game_icon(game_key: str):
            if game_key == 'forza':
                t = _forza_pixmap(16)
                return QIcon(t) if t is not None else None
            if game_key == 'rocketleague':
                p = os.path.join(_ASSETS, 'rocketleague.png')
            else:
                return None
            if p and os.path.exists(p):
                return QIcon(QPixmap(p).scaled(_s(16), _s(16), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return None

        rows = []
        for name in sorted(all_presets):
            tag = _presets.preset_game(all_presets[name])
            if tag and tag != cur_game:
                continue
            rows.append((name, tag))
        if rows:
            m.addSeparator()
            for name, tag in rows:
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(_s(6), _s(2), _s(6), _s(2))
                h.setSpacing(_s(4))
                load_btn = QPushButton('  ' + name)
                load_btn.setObjectName('popupitem')
                load_btn.setFont(_ui_font(14))
                load_btn.setCursor(Qt.PointingHandCursor)
                ic = _game_icon(tag)
                if ic is not None:
                    load_btn.setIcon(ic)
                    load_btn.setIconSize(QSize(_s(16), _s(16)))
                load_btn.clicked.connect(lambda _=False, n=name: (self._load_preset(n), _close_all()))
                del_btn = QPushButton()
                del_btn.setObjectName('presetdel')
                del_btn.setIcon(QIcon(_trash_pixmap(15)))
                del_btn.setIconSize(QSize(_s(15), _s(15)))
                del_btn.setFixedWidth(_s(32))
                del_btn.setCursor(Qt.PointingHandCursor)
                del_btn.setToolTip(f"Delete '{name}'")
                del_btn.clicked.connect(lambda _=False, n=name: self._delete_preset(n))
                h.addWidget(load_btn, 1)
                h.addWidget(del_btn)
                wa = QWidgetAction(m)
                wa.setDefaultWidget(row)
                m.addAction(wa)
                self._preset_row_actions[name] = wa
        m.addSeparator()
        folder_act = m.addAction('Open presets folder')
        folder_act.triggered.connect(self._open_presets_folder)
        if getattr(self, '_ovl_pre_menu', None) is not None:
            m.addSeparator()
            m.addMenu(self._ovl_pre_menu)
            return None
        return None

    def _delete_preset(self, name: str):
        _presets.delete_preset(name)
        wa = getattr(self, '_preset_row_actions', {}).pop(name, None)
        if wa is not None:
            self._presets_menu.removeAction(wa)
        self._presets_menu_sig = None
        return None

    def _open_presets_folder(self):
        """Open the presets/ folder in Explorer (one .json per preset), so
        the user can back up / copy / share individual preset files."""
        folder = _presets.presets_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
            return None
        except Exception:
            try:
                import subprocess
                subprocess.Popen(['explorer', folder])
                return None
            except Exception:
                return None

    def _save_preset(self):
        name = self._ask_preset_name()
        if not name:
            return None
        _presets.save_preset(name, _presets.capture(self._cfg))
        return None

    def _ask_preset_name(self) -> str:
        dlg = QDialog(self)
        dlg.setWindowTitle('Save preset')
        if os.path.exists(_APP_ICON):
            dlg.setWindowIcon(QIcon(_APP_ICON))
        dlg.setStyleSheet(_build_qss(_CHECK))
        dlg.setFont(_ui_font(14))
        dlg.setMinimumWidth(_s(320))
        v = QVBoxLayout(dlg)
        v.setContentsMargins(_s(20), _s(18), _s(20), _s(16))
        v.setSpacing(_s(10))
        t = QLabel('Name this preset')
        t.setFont(_ui_font(16, QFont.Bold))
        t.setStyleSheet(f'color: {_c("text")};')
        v.addWidget(t)
        edit = QLineEdit()
        edit.setFont(_ui_font(14))
        edit.setPlaceholderText('e.g. Loud, Chill, Late night…')
        v.addWidget(edit)
        btns = QHBoxLayout()
        btns.setSpacing(_s(8))
        cancel = QPushButton('Cancel')
        cancel.setObjectName('togglebtn')
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton('Save')
        ok.setObjectName('savebtn')
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(dlg.accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)
        edit.returnPressed.connect(dlg.accept)
        edit.setFocus()
        dlg.raise_()
        dlg.activateWindow()
        if dlg.exec() != QDialog.Accepted:
            return ''
        return edit.text().strip()

    def _load_preset(self, name: str):
        data = _presets.load_presets().get(name)
        if not data:
            return None
        self._apply_preset(data)
        return None

    _PRESET_FIELD_GATE = {
        'menu_level': 'menu_volume',
        'unfocused_level': 'unfocused_volume',
        'duck_level': 'ducked_volume',
        'ducking_enabled': 'speech_recognition',
        'low_cpu_mode': 'save_cpu',
        'volume_ramp_in': 'fade_length',
    }
    def _apply_preset(self, data: dict):
        from fh6_spotify import game_presets as _gp
        cur_preset = self._cfg.game_preset
        for field in _presets.PRESET_FIELDS:
            if field not in data:
                continue
            gate = self._PRESET_FIELD_GATE.get(field)
            if gate is not None and not _gp.show_control(cur_preset, gate):
                continue
            setattr(self._cfg, field, data[field])
        vol_slider = self._sliders.get('vol')
        if vol_slider is not None:
            vol_slider.setValue(int(round(self._cfg.full_level * 100)))
        self._sliders['menu'].setValue(int(round(self._cfg.menu_level * 100)))
        self._sliders['duck'].setValue(int(round(self._cfg.duck_level * 100)))
        self._sliders['fade'].setValue(max(100, min(2000, _ms_from_ramp(self._cfg.volume_ramp_in))))
        unfoc = self._sliders.get('unfocused')
        if unfoc is not None:
            unfoc.setValue(int(round(self._cfg.unfocused_level * 100)))
        self._overlay_cb.setChecked(self._cfg.overlay_enabled)
        self._duck_cb.setChecked(self._cfg.ducking_enabled)
        self._duck_row.setEnabled(self._cfg.ducking_enabled)
        if hasattr(self, '_duckscope_cb'):
            self._duckscope_cb.setChecked(getattr(self._cfg, 'duck_scope', 'game') == 'system')
            self._duckscope_cb.setEnabled(self._cfg.ducking_enabled)
        if hasattr(self, '_ownvoice_cb'):
            self._ownvoice_cb.setChecked(getattr(self._cfg, 'duck_on_own_voice', False))
            self._ownvoice_cb.setEnabled(self._cfg.ducking_enabled and getattr(self._cfg, 'duck_scope', 'game') == 'system')
        if hasattr(self, '_mic_row'):
            self._mic_combo.setEnabled(self._ownvoice_cb.isEnabled() and self._ownvoice_cb.isChecked())
        self._lowcpu_cb.setChecked(False)
        if hasattr(self, '_overlay_size_slider'):
            self._overlay_size_slider.setValue(int(round(self._cfg.overlay_scale * 100)))
        if hasattr(self, '_only_cover_cb'):
            self._only_cover_cb.setChecked(self._cfg.overlay_compact)
        if hasattr(self, '_ingame_cb'):
            self._ingame_cb.setChecked(getattr(self._cfg, 'overlay_in_game_only', False))
        if hasattr(self, '_drive_cb'):
            self._drive_cb.setChecked(getattr(self._cfg, 'overlay_drive_only', False))
        self._bind_label.setText(self._binds_text())
        self._queue_save()
        try:
            self._apply_preset_visibility()
        except Exception:
            pass
        self._last_seen_preset = self._cfg.game_preset
        return None

    def _set_scale(self, step: float):
        self._cfg.ui_scale = step
        self._queue_save()
        if self._on_scale:
            self._on_scale()
            return None

    def _load_source_icons(self):
        """(Re)build the theme-tinted source / status / link pixmaps. Called from
        __init__ and on a live theme switch (_retheme)."""
        self._forza_on = _forza_pixmap(28) or _app_icon(_FORZA, 'F', 28)
        self._forza_off = _dim_pixmap(self._forza_on)
        self._spot_on = _app_icon(_SPOTIFY, 'S', 28)
        self._spot_off = _dim_pixmap(self._spot_on)
        self._browser_on = _tinted(os.path.join(_ASSETS, 'browser_white.png'), _c('icon'), 28) or _globe_pixmap(28)
        self._browser_off = _dim_pixmap(self._browser_on)
        self._am_on = _app_icon(os.path.join(_ASSETS, 'applemusic.png'), 'M', 28)
        self._am_off = _dim_pixmap(self._am_on)
        self._lm_on = _load_scaled(os.path.join(_ASSETS, 'localmedia.png'), 28) or _folder_pixmap(28)
        self._lm_off = _dim_pixmap(self._lm_on)
        self._tidal_on = _zoom_icon(os.path.join(_ASSETS, 'tidal.png'), _s(28), 1.3) or _app_icon('', 'T', 28)
        self._tidal_off = _dim_pixmap(self._tidal_on)
        self._amazon_on = _load_scaled(os.path.join(_ASSETS, 'amazonmusic.png'), 28) or _app_icon('', 'A', 28)
        self._amazon_off = _dim_pixmap(self._amazon_on)
        self._ytm_on = _load_scaled(os.path.join(_ASSETS, 'ytmusic.png'), 28) or _app_icon('', 'Y', 28)
        self._ytm_off = _dim_pixmap(self._ytm_on)
        _link_col = '#9a9a98' if _active_theme() == 'light' else _c('icon')
        self._link_on = _tinted(_LINK, _link_col, 30) or _load_scaled(_LINK, 30) or _link_pixmap(True)
        self._link_off = _tinted(_LINK_BROKEN, _c('icon_dim'), 32) or _load_scaled(_LINK_BROKEN, 32) or _dim_pixmap(self._link_on)
        self._link_glow = _tinted(_LINK, '#1DB954', 30) or self._link_on
        return None

    def _load_play_icons(self):
        """(Re)build the theme-tinted play / pause glyph icons (glyph-on-pill
        colour). Called from __init__ and on a live theme switch."""
        _PM = getattr(self, '_pm_size', 30)
        _play_src = _load_scaled(os.path.join(_ASSETS, 'play icon.png'), int(_PM * 0.47))
        if _play_src is not None and not _play_src.isNull():
            _tp = QPainter(_play_src)
            _tp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            _tp.fillRect(_play_src.rect(), QColor(_c('btn_text')))
            _tp.end()
            _pp = QPixmap(_s(_PM), _s(_PM))
            _pp.fill(QColor(0, 0, 0, 0))
            _q = QPainter(_pp)
            _q.drawPixmap((_s(_PM) - _play_src.width()) // 2 + _s(1), (_s(_PM) - _play_src.height()) // 2, _play_src)
            _q.end()
            self._icon_play = QIcon(_pp)
        else:
            self._icon_play = QIcon(_media_pixmap('play', _c('emph_text'), _PM))
        _pause_src = _load_scaled(os.path.join(_ASSETS, 'pause icon v2.png'), int(_PM * 0.45))
        if _pause_src is not None and not _pause_src.isNull():
            _tz = QPainter(_pause_src)
            _tz.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            _tz.fillRect(_pause_src.rect(), QColor(_c('btn_text')))
            _tz.end()
            _pz = QPixmap(_s(_PM), _s(_PM))
            _pz.fill(QColor(0, 0, 0, 0))
            _qz = QPainter(_pz)
            _qz.drawPixmap((_s(_PM) - _pause_src.width()) // 2, (_s(_PM) - _pause_src.height()) // 2, _pause_src)
            _qz.end()
            self._icon_pause = QIcon(_pz)
            return None
        self._icon_pause = QIcon(_media_pixmap('pause', _c('emph_text'), _PM))
        return None

    def _retheme(self):
        """Recolor the WHOLE window in place for a live theme switch - no window
        rebuild (which blinked / 'loaded in'). QSS handles most widgets; the
        theme-tinted pixmaps are regenerated + re-set here. Updates are suspended
        so the swap lands in a single repaint."""
        self.setUpdatesEnabled(False)
        self._src_menu_cached = None
        self._src_menu_pm = None
        self._last_cover_key = None
        self._active_slider = None
        try:
            self.setStyleSheet(_build_qss(_CHECK))
            self._load_source_icons()
            self._link_blend_cache = {}
            self._load_play_icons()
            self._btn_prev.setIcon(QIcon(_media_pixmap('prev', _c('icon'), 22)))
            self._btn_next.setIcon(QIcon(_media_pixmap('next', _c('icon'), 22)))
            _caret_col = _c('icon_dim') if _active_theme() == 'light' else _c('icon')
            self._caret_pm = _tinted(os.path.join(_ASSETS, 'down-chevron.png'), _caret_col, 12)
            self._dev_wm_big = _dev_wm_rot(self._cfg.input_device)
            self._ic_mixer = _tab_icon('mixer', 20)
            self._ic_mixer_dull = _tab_icon('mixer', 20, _c('icon_dim'))
            self._ic_extras = _tab_icon('extras', 20)
            self._ic_extras_dull = _tab_icon('extras', 20, _c('icon_dim'))
            _unl = _tinted(os.path.join(_ASSETS, 'unlocked lock.png'), _c('text_hint'), 19)
            self._lock_btn._off_icon = QIcon(_unl) if _unl is not None else _kind_icon('lock', _c('text_hint'))
            self._lock_btn._on_icon = _kind_icon('lock', _c('emph_text'))
            self._lock_btn.setIcon(self._lock_btn._on_icon if self._lock_btn.property('active') == 'true' else self._lock_btn._off_icon)
            self._power_btn._retint()
            _vz = QPixmap(_s(40), _s(40))
            _vz.fill(QColor(0, 0, 0, 0))
            _vp = QPainter(_vz)
            _vp.setRenderHint(QPainter.Antialiasing)
            _vp.setPen(Qt.NoPen)
            _vp.setBrush(QColor(_c('icon')))
            _bw = _s(4)
            for _i, _bh in enumerate((0.38, 0.72, 0.52, 0.86, 0.46)):
                _x = _s(4) + _i * (_bw + _s(3))
                _hh = int(_s(40) * _bh * 0.78)
                _vp.drawRoundedRect(_x, (_s(40) - _hh) // 2, _bw, _hh, _bw * 0.45, _bw * 0.45)
            _vp.end()
            self._viz_btn.setIcon(QIcon(_vz))
            if getattr(self, '_mic_lbl', None) is not None:
                self._mic_lbl.setPixmap(_mic_pixmap(15, _c('text_hint')))
            if getattr(self, '_move_overlay_btn', None) is not None:
                self._move_overlay_btn.setIcon(QIcon(_move_pixmap(self._move_sz, _c('text_hint'))))
            if getattr(self, '_overlay_size_reset', None) is not None:
                self._overlay_size_reset.setIcon(QIcon(_undo_pixmap(20, _c('text_hint'))))
            if getattr(self, '_menu_btn', None) is not None:
                self._menu_btn.setIcon(_menu_icon())
            if getattr(self, '_verbtn', None) is not None:
                self._verbtn._retint()
            if getattr(self, '_btn_min', None) is not None:
                self._btn_min.setIcon(_caption_icon('min'))
                self._btn_max.setIcon(_caption_icon('restore' if self.isMaximized() else 'max'))
                self._btn_close._ico_rest = _caption_icon('close')
                self._btn_close.setIcon(self._btn_close._ico_rest)
            if getattr(self, '_foot_lbl', None) is not None and getattr(self, '_foot_html', None):
                self._foot_lbl.setText(self._foot_html())
            if getattr(self, '_foot_disc', None) is not None:
                _dp = _tinted(os.path.join(_ASSETS, 'Discord_logo_blue.png'), _c('icon'), 16)
                if _dp is not None:
                    self._foot_disc.setPixmap(_dp)
            self._brand_logo = QPixmap(os.path.join(_ASSETS, 'Segue logo in bottom ui.png'))
            self._brand_pm = None
            self._brand_bucket = None
            _old_menu = getattr(self, '_menu', None)
            self._build_menu()
            if _old_menu is not None:
                _old_menu.deleteLater()
            if not getattr(self, '_last_thumb', None) and getattr(self, '_cover', None) is not None:
                self._cover.setPixmap(_cover_placeholder(self._cover_sz, _s(8)))
            for _fn in getattr(self, '_themed', []):
                try:
                    _fn()
                except Exception:
                    pass
            self._update_tabs()
            self._refresh_status()
            self._apply_source_icon()
        finally:
            self.setUpdatesEnabled(True)
        self.update()
        return None

    def _open_preferences(self):
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance()
        existing = getattr(_app, '_segue_prefs', None)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return None
            except Exception:
                pass
        dlg = _PreferencesDialog(None, cfg=self._cfg, ui=self._ui)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        _app._segue_prefs = dlg
        dlg.destroyed.connect(lambda: setattr(_app, '_segue_prefs', None))
        _apply_dwm_titlebar(dlg)

        def _pick(name):
            if name == _load_theme():
                return None
            _save_theme(name)
            self._retheme()
            dlg.setStyleSheet(_build_qss(_CHECK))
            dlg.refresh_selected(name)
            _apply_dwm_titlebar(dlg)
            return None

        dlg.picked.connect(_pick)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return None

    def _rebuild_ovl_presets(self):
        """Populate the 'Overlay preset' submenu from the saved presets (config)."""
        m = self._ovl_pre_menu
        m.clear()
        presets = dict(getattr(self._cfg, 'overlay_presets', {}) or {})
        active = getattr(self._cfg, 'overlay_preset_name', '') or ''
        from PySide6.QtGui import QActionGroup
        grp = QActionGroup(self)
        grp.setExclusive(True)
        da = m.addAction('Default')
        da.setCheckable(True)
        da.setChecked(active == 'Default' or not active)
        grp.addAction(da)
        da.triggered.connect(lambda _=False: self._pick_ovl_preset('Default'))
        if presets:
            m.addSeparator()
            for name in sorted(presets):
                a = m.addAction(name)
                a.setCheckable(True)
                a.setChecked(name == active)
                grp.addAction(a)
                a.triggered.connect(lambda _=False, n=name: self._pick_ovl_preset(n))

    def _pick_ovl_preset(self, name):
        """Make a saved preset the active overlay (live for OBS + persisted)."""
        import copy
        if name == 'Default':
            pr = {}
        else:
            presets = getattr(self._cfg, 'overlay_presets', {}) or {}
            if name not in presets:
                return None
            pr = copy.deepcopy(presets[name])
        self._cfg.overlay_preset = pr
        self._cfg.overlay_preset_name = name
        try:
            from fh6_spotify.config import default_config_path
            self._cfg.save(default_config_path())
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QApplication
            srv = QApplication.instance()._segue.get('overlay_srv')
            if srv:
                srv.set_preset(pr)
                srv.preset_name = name
                if getattr(srv, '_httpd', None) is not None:
                    srv._httpd.preset_name = name
                    return None
        except Exception:
            return None

    def _open_overlay_editor(self):
        """Hamburger -> Stream overlay: live editor for the OBS browser-source
        overlay (enable, URL, style). Changes apply to the running server instantly."""
        try:
            dlg = _StreamOverlayEditor(self._cfg, self)
            _apply_dwm_titlebar(dlg)
            dlg.exec()
            return None
        except Exception as exc:
            print(f'overlay editor failed: {exc}')
            return None

    def _paint_controls_pill(self, btn, e):
        """Paint the device watermark as part of a CENTRED [text + controller]
        cluster: the layout centres the text (with the watermark width reserved),
        and this paints the glyph just right of that centred text so the pair
        reads as one balanced group, not text-left / glyph-far-right."""
        w = max(1, btn.width())
        tw = getattr(self, '_pill_text_w', 0)
        hfrac = min(0.92, 0.5 + (tw + _s(4)) / (2.0 * w))
        hsc = _dev_wm_scale(self._cfg.input_device) * 1.333
        _paint_pill_backdrop(btn, e, getattr(self, '_dev_wm_big', None), _s(8), opacity=0.16, hfrac=hfrac, hscale=hsc, vfrac=0.5, fade=True)
        return None

    def _refresh_controls_pill(self):
        """Relabel + re-watermark the Controls pill after a live device hot-swap
        (no restart). Mirrors the build in __init__; called from _RebindDialog
        once the shared config's input_device has changed."""
        dev = self._cfg.input_device
        self._dev_wm_big = _dev_wm_rot(dev)
        if getattr(self, '_rdev_lbl', None) is not None:
            self._rdev_lbl.setText(_dev_name(dev))
            _bf = QFont(self._hint_font)
            _bf.setBold(True)
            self._pill_text_w = QFontMetrics(self._hint_font).horizontalAdvance('Rebind controls') + _s(5) + QFontMetrics(_ui_font(21, QFont.Bold)).horizontalAdvance('·') + _s(5) + QFontMetrics(_bf).horizontalAdvance(_dev_name(dev))
        btn = getattr(self, '_controls_btn', None)
        if btn is not None:
            btn.setToolTip(f'Using {_dev_name(dev)}. Click to view all controls and rebind buttons.')
            btn.update()
            return None

    def _open_rebind(self):
        dlg = _RebindDialog(self._cfg, self._save, self._on_restart, self._ui, self)
        dlg.exec()
        return None

    def _open_help(self):
        dev = self._cfg.input_device
        mode = self._cfg.mode
        preset = self._cfg.game_preset
        sig = (dev, mode, preset)
        if getattr(self, '_help', None) is None or getattr(self, '_help_sig', None) != sig:
            if getattr(self, '_help', None) is not None:
                try:
                    self._help.close()
                except Exception:
                    pass
            self._help = _HelpWindow(input_device=dev, mode=mode, on_open_controls=self._open_rebind, preset=preset)
            self._help_sig = sig
        self._help.show()
        _apply_dwm_titlebar(self._help)
        self._help.raise_()
        self._help.activateWindow()
        return None

    def _maybe_run_tour(self, force: bool = False):
        """First-run guided spotlight tour. Welcome modal -> 6 highlighted steps.
        Only the tour COMPLETING marks tour_done - if the user dismisses the
        welcome dialog (esc / close / window-X) we leave the flag alone so the
        tour reshows next launch instead of vanishing on a stray click.
        force=True skips the tour_done gate (hamburger menu -> Show intro)."""
        if not self._cfg.tour_reset_migration_done:
            self._cfg.tour_done = False
            self._cfg.tour_reset_migration_done = True
            self._queue_save()
        if not force:
            if self._cfg.tour_done or getattr(self, '_tour_started', False):
                return None
        self._tour_started = True
        if not force:
            if _WelcomeDialog(self).exec() != QDialog.Accepted:
                self._cfg.tour_done = True
                self._save()
                return None
        _is_forza = getattr(self._cfg, 'game_preset', 'forza') == 'forza'
        self._tour = _TourOverlay(self, self._tour_steps(), self._mark_tour_done, on_finish_cta=self._open_setup_help if _is_forza else None, final_cta_label='Done', final_secondary_label='Setup' if _is_forza else 'Skip')
        return None

    def _open_setup_help(self):
        """Open Help and jump straight to the Setup section (used as final-step
        CTA so first-run users land in the install guide)."""
        self._open_help()
        try:
            nav = self._help.findChild(QListWidget)
            if nav is not None:
                nav.setCurrentRow(0)
                return None
        except Exception:
            return None

    def _tour_steps(self):
        return [
            (self._conn_pane, 'Connections', 'Game + music status. Both turn green when connected.<br><br>◂ next to the game icon = switch game.<br>▾ next to the source icon = switch music source.'),
            (self._np_area, 'Now playing', 'Your current track + cover art.<br><br>Click the source icon up top to switch Spotify ↔ browser.'),
            ([self._tab_mixer, self._tab_extras], 'Mixer & Extras', '<b>Mixer</b>: the volume sliders.<br><br><b>Extras</b>: fade length, overlay, Open-with games.'),
            ([self._btn_prev, self._btn_play, self._btn_next], 'Media transport', 'Previous / play-pause / next.<br><br>Drives Spotify (or your browser) via Windows media keys.'),
            ([self._controls_btn, self._lock_btn, self._power_btn], 'Bottom controls', '<b>Controls</b>: pick and rebind your device.<br><b>Lock Skip</b>: freeze track-skipping.<br><b>Power</b>: pause Segue without closing.'),
            (self._menu_btn, 'Menu', 'App scale, Presets, Help, Quit.'),
        ]

    def _mark_tour_done(self):
        self._cfg.tour_done = True
        self._save()
        return None

    def _confirm_quit(self) -> bool:
        if getattr(self._cfg, 'skip_quit_confirm', False):
            return True
        dlg = _ConfirmDialog(self, 'Quit Segue?', "This stops Segue controlling your music and restores Spotify's volume.", confirm_text='Quit Segue', cancel_text='Cancel', show_dont_ask=True)
        ok = dlg.exec() == QDialog.Accepted
        if ok and dlg.dont_ask:
            try:
                self._cfg.skip_quit_confirm = True
                self._cfg.save(self._path)
            except Exception:
                pass
        return ok

    def _menu_quit(self):
        if self._confirm_quit():
            self._quitting = True
            self._on_close()
            return None

    def _menu_uninstall(self):
        """Launch the Inno uninstaller next to the frozen exe, then quit so the
        uninstaller can remove our own files. Falls back to a friendly notice
        when running from source (no unins000.exe to find)."""
        import subprocess
        exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else None
        unins = ''
        if exe_dir:
            candidates = [os.path.join(exe_dir, '_uninstall', 'unins000.exe'), os.path.join(exe_dir, 'unins000.exe')]
            for cand in candidates:
                if os.path.exists(cand):
                    unins = cand
                    break
            if not unins:
                for root, _dirs, files in os.walk(exe_dir):
                    if 'unins000.exe' in files:
                        unins = os.path.join(root, 'unins000.exe')
                        break
        if not unins:
            dlg = _ConfirmDialog(self, title='Uninstall not available', body="No installer found. You're running Segue from source - uninstall is only available in the installed build.", confirm_text='OK', cancel_text='')
            dlg.exec()
            return None
        dlg = _ConfirmDialog(self, title='Uninstall Segue?', body='This will close Segue and launch the uninstaller. Your settings (in %APPDATA%\\Segue) will also be removed.', confirm_text='Uninstall', cancel_text='Cancel')
        if dlg.exec() != QDialog.Accepted:
            return None
        try:
            subprocess.Popen([unins], close_fds=True, creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0))
            self._quitting = True
            self._on_close()
            return None
        except Exception:
            return None

    def request_quit(self):
        """Entry point for the tray 'Quit' action (confirms first)."""
        self._menu_quit()
        return None

    def _playpause(self):
        """Toggle play/pause on the chosen source - same routed path as the play
        button (SMTC-filtered to the selected source; global media key fallback)."""
        cb = (self._ui or {}).get('routed_playpause')
        (cb or _mk.media_playpause)()
        return None

    def _taskbar_action(self, kind):
        """Route a taskbar thumbnail-button click through the same path as the media buttons."""
        ui = self._ui or {}
        if kind == 'prev':
            (ui.get('routed_prev') or _mk.media_prev)()
            return None
        if kind == 'next':
            (ui.get('routed_next') or _mk.media_next)()
            return None
        (ui.get('routed_playpause') or _mk.media_playpause)()
        return None

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Show and isinstance(obj, QDialog) and obj.isWindow():
            _apply_dwm_titlebar(obj)
        if e.type() == QEvent.KeyPress and e.key() == Qt.Key_Space and not e.isAutoRepeat() and self.isActiveWindow():
            from PySide6.QtWidgets import QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit
            fw = self.focusWidget()
            if not isinstance(fw, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)):
                self._playpause()
                return True
        return super().eventFilter(obj, e)

    def closeEvent(self, e):
        if self._quitting:
            e.accept()
            return None
        if getattr(self, '_overlay_moving', False):
            self._on_move_overlay()
        if getattr(self._cfg, 'close_to_tray', True):
            e.ignore()
            self.hide()
            self._notify_tray_once()
            return None
        if self._confirm_quit():
            self._quitting = True
            e.accept()
            self._on_close()
            return None
        e.ignore()
        return None

    def _notify_tray_once(self):
        """First time the window is closed to tray, tell the user it's still
        running (so they don't think it quit). Shown once ever."""
        if getattr(self._cfg, 'tray_hint_seen', False):
            return None
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon
            seg = getattr(QApplication.instance(), '_segue', None)
            tray = seg.get('tray') if seg else None
            if tray is not None:
                tray.showMessage('Segue is still running', 'Closed to the tray, your music keeps mixing. Right-click the tray icon to quit.', QSystemTrayIcon.MessageIcon.Information, 6000)
            self._cfg.tray_hint_seen = True
            self._queue_save()
            return None
        except Exception:
            return None

    def _slider_row(self, label, lo, hi, value, cb, tip='', key=None, fmt=None):
        fmt = fmt or (lambda x: f'{x}%')
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(_s(6))
        name = QLabel(label)
        name.setFont(_ui_font(15))
        val = QLabel(fmt(value))
        val.setObjectName('hint')
        val.setFont(self._hint_font)
        head.addWidget(name)
        if tip:
            head.addWidget(_InfoLabel(tip, 15))
        head.addStretch(1)
        head.addWidget(val)
        v.addLayout(head)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        s.setCursor(Qt.PointingHandCursor)
        s.valueChanged.connect(lambda x: (val.setText(fmt(x)), cb(x)))
        v.addWidget(s)
        if key is not None:
            self._sliders[key] = s
            self._slider_vals[key] = val
            if not hasattr(self, '_slider_names'):
                self._slider_names = {}
            self._slider_names[key] = name
        return row

    def _on_link_glow(self, v):
        """Drive the chain glow + green tint together from one 0..1 value, so the
        icon eases greener exactly as the drop-shadow glows."""
        v = float(v)
        try:
            self._link_glow_fx.setBlurRadius(v * 22.0)
        except Exception:
            pass
        out = QPixmap(self._link_on.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(0, 0, self._link_on)
        _gv = v * 2.0 if _active_theme() == 'light' else v
        p.setOpacity(min(1.0, _gv))
        p.drawPixmap(0, 0, self._link_glow)
        p.end()
        self._conn_lbl.setPixmap(out)
        return None

    def _start_link_pulse(self):
        """Chain the looping pulse on once the entrance one-shot finishes (only if
        still connected)."""
        if not getattr(self, '_link_was_linked', False):
            return None
        try:
            self._link_pulse.stop()
            self._link_pulse.start()
            return None
        except Exception:
            return None

    def _link_pixmap_now(self) -> QPixmap:
        """Return the chain pixmap for the current point in the post-connect
        fade. t=0 -> fully green-tinted (_link_glow), t=1 -> plain white
        (_link_on). OutCubic ease so the bulk of the colour shift happens
        late, mirroring the drop-shadow glow's tail."""
        if not self._link_fade_start:
            return self._link_on
        elapsed = time.monotonic() - self._link_fade_start
        FADE_S = 7.0
        if elapsed >= FADE_S:
            return self._link_on
        if elapsed <= 0.0:
            return self._link_glow
        linear = elapsed / FADE_S
        eased = 1.0 - (1.0 - linear) ** 3
        bucket = round(eased * 20)
        cached = self._link_blend_cache.get(bucket)
        if cached is not None:
            return cached
        t = bucket / 20.0
        out = QPixmap(self._link_on.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setOpacity(1.0 - t)
        p.drawPixmap(0, 0, self._link_glow)
        p.setOpacity(t)
        p.drawPixmap(0, 0, self._link_on)
        p.end()
        self._link_blend_cache[bucket] = out
        return out

    def _on_source_click(self):
        """Icon button = open the music source: foreground its window if it's
        already running (open OR minimized), else launch it. The chevron opens the
        picker."""
        self._bring_source_to_front()
        return None

    def _bring_source_to_front(self):
        """Foreground the source's window if one exists (matched by TITLE, so UWP
        apps like Apple Music - whose window lives in ApplicationFrameHost - work
        too), restoring it if minimized. If there's no window, launch it and
        foreground it once it appears. Foregrounding runs on the GUI thread, which
        holds the foreground right after the click."""
        from fh6_spotify import source_launch as _sl
        cfg = self._cfg
        needles = [n for n in (_sl.recipe_for(cfg)[0],) if n]
        if needles and _sl._foreground_by_title(needles):
            return None
        self._launch_source_async()
        for ms in (700, 1500, 2600, 4000):
            QTimer.singleShot(ms, lambda n=needles: _sl._foreground_by_title(n))
        return None

    def _game_launch_names(self):
        """Title / launch name candidates for the current game preset. 'Forza
        Horizon' matches the FH4/5/6 window titles; 'other' uses the picked
        process's base name."""
        from fh6_spotify import game_presets as _gp
        key = getattr(self._cfg, 'game_preset', 'forza')
        if key == 'other':
            t = (getattr(self._cfg, 'general_target_process', '') or '').rsplit('.', 1)[0]
            return [t] if t else []
        label = _gp.label_for(key)
        return [label] if label else []

    def _bring_game_to_front(self):
        """Game icon click: foreground the game window if it's running (matched by
        title, incl. minimized), else best-effort launch it by name. Mirrors the
        source icon."""
        from fh6_spotify import source_launch as _sl
        import threading
        names = self._game_launch_names()
        if names and _sl._foreground_by_title(names):
            return None
        if not names:
            return None
        self._start_game_launch_spinner()

        def _run(n=names):
            try:
                lnk = _sl.find_start_menu_shortcut(n)
                if lnk:
                    os.startfile(lnk)
                    return None
                aumid = _sl._resolve_aumid(n)
                if aumid:
                    _sl._open_appsfolder(aumid)
                    return None
                appid = _sl._resolve_steam_appid(n)
                if appid:
                    os.startfile(f'steam://rungameid/{appid}')
                    return None
            except Exception:
                return None

        threading.Thread(target=_run, daemon=True).start()
        for ms in (1500, 4000, 8000, 14000):
            QTimer.singleShot(ms, lambda n=names: _sl._foreground_by_title(n))
        return None

    def _on_source_caret_click(self):
        """Dropdown-caret button: always open the source picker."""
        self._source_menu()
        return None

    def _launch_source_async(self):
        """Open the configured source app off the GUI thread (the Start-Menu
        shortcut walk can take a moment). Best-effort; no-op if already running.
        Shows a spinner on the source button until the app is detected (or a
        timeout), so the click gives feedback."""
        import threading
        from PySide6.QtCore import QTimer
        b = self._spot_lbl
        b._launching = True
        b._spin_angle = 0
        b._launch_deadline = time.monotonic() + 18.0
        if getattr(self, '_launch_spin_timer', None) is None:
            self._launch_spin_timer = QTimer(self)
            self._launch_spin_timer.setInterval(16)
            self._launch_spin_timer.setTimerType(Qt.PreciseTimer)
            self._launch_spin_timer.timeout.connect(self._tick_launch_spinner)
        self._launch_spin_timer.start()
        self._apply_source_icon()
        cfg = self._cfg

        def _run():
            try:
                from fh6_spotify import source_launch as _sl
                _sl.launch_source(cfg, to_front=True)
                return None
            except Exception:
                return None

        threading.Thread(target=_run, daemon=True).start()
        return None

    def _spinner_targets(self):
        """(button, apply-icon-fn) pairs that can show a launch spinner. One shared
        timer drives both the source and the game icon."""
        return ((getattr(self, '_spot_lbl', None), self._apply_source_icon), (getattr(self, '_forza_lbl', None), self._apply_game_icon))

    def _start_game_launch_spinner(self):
        """Spin the game icon while a game boots. Long deadline - Steam -> game can
        take a while; _refresh_status stops it sooner once the game is detected."""
        from PySide6.QtCore import QTimer
        b = getattr(self, '_forza_lbl', None)
        if b is None:
            return None
        b._launching = True
        b._spin_angle = 0
        b._launch_deadline = time.monotonic() + 150.0
        if getattr(self, '_launch_spin_timer', None) is None:
            self._launch_spin_timer = QTimer(self)
            self._launch_spin_timer.setInterval(16)
            self._launch_spin_timer.setTimerType(Qt.PreciseTimer)
            self._launch_spin_timer.timeout.connect(self._tick_launch_spinner)
        self._launch_spin_timer.start()
        self._apply_game_icon()
        return None

    def _tick_launch_spinner(self):
        any_spin = False
        for b, apply in self._spinner_targets():
            if b is None or not getattr(b, '_launching', False):
                continue
            if time.monotonic() >= getattr(b, '_launch_deadline', 0):
                b._launching = False
                apply()
                continue
            b._spin_angle = (getattr(b, '_spin_angle', 0) + 8) % 360
            apply()
            any_spin = True
        if not any_spin:
            if getattr(self, '_launch_spin_timer', None) is not None:
                self._launch_spin_timer.stop()
                return None
            return None

    def _stop_launch_spinner(self, b=None, apply=None):
        b = b if b is not None else getattr(self, '_spot_lbl', None)
        apply = apply if apply is not None else self._apply_source_icon
        if b is not None and getattr(b, '_launching', False):
            b._launching = False
            apply()
        if not any(((x is not None) and getattr(x, '_launching', False) for x, _ in self._spinner_targets())):
            if getattr(self, '_launch_spin_timer', None) is not None:
                self._launch_spin_timer.stop()
                return None
            return None

    def _apply_source_icon(self):
        """Set the source button's icon. While launching -> source art dimmed
        under a spinner; hover + launchable -> source art dimmed under a power
        glyph; otherwise the plain source icon. The source stays visible behind
        the launch affordance instead of being replaced by it."""
        b = getattr(self, '_spot_lbl', None)
        if b is None:
            return None
        src = getattr(self, '_src_icon', QIcon())
        box = b.iconSize().width()
        if getattr(b, '_launching', False):
            _dx = -1 if getattr(self._cfg, 'source', '') == 'spotify' else 0
            b.setIcon(_launcher_icon(src, box, spinner=True, angle=getattr(b, '_spin_angle', 0), dx=_dx))
            return None
        if getattr(b, '_launchable', False) and b.underMouse():
            b.setIcon(_launcher_icon(src, box, glow=getattr(b, '_launch_glow', 0.4)))
            return None
        b.setIcon(src)
        return None

    def _apply_game_icon(self):
        """Set the game button's icon. Hover + launchable (game not running) -> game
        art dimmed under a power glyph; otherwise the plain game icon. Mirrors
        _apply_source_icon (the game keeps the same launch affordance as the source)."""
        b = getattr(self, '_forza_lbl', None)
        if b is None:
            return None
        base = getattr(self, '_game_icon', QIcon())
        box = b.iconSize().width()
        if getattr(b, '_launching', False):
            b.setIcon(_launcher_icon(base, box, spinner=True, angle=getattr(b, '_spin_angle', 0)))
            return None
        if getattr(b, '_launchable', False) and b.underMouse():
            b.setIcon(_launcher_icon(base, box, glow=getattr(b, '_launch_glow', 0.4)))
            return None
        b.setIcon(base)
        return None

    def _on_source_move(self, b, e):
        """Track which side of the source button the cursor is over while it's a
        launcher: over the icon/click side -> ease the launch glyph bright; toward
        the caret (menu) side -> ease it back down. Only re-animates on a side
        change, so it's a smooth lerp, not a per-pixel jump."""
        from PySide6.QtWidgets import QPushButton
        QPushButton.mouseMoveEvent(b, e)
        if not getattr(b, '_launchable', False):
            return None
        side = 'icon' if e.position().x() <= b.width() * 0.55 else 'caret'
        if side != getattr(b, '_launch_side', None):
            b._launch_side = side
            self._animate_launch_glow(b, 1.0 if side == 'icon' else 0.35)

    def _animate_launch_glow(self, b, target):
        from PySide6.QtCore import QVariantAnimation, QEasingCurve
        prev = getattr(b, '_launch_glow_anim', None)
        if prev is not None:
            prev.stop()
        anim = QVariantAnimation(b)
        anim.setDuration(160)
        anim.setStartValue(float(getattr(b, '_launch_glow', 0.4)))
        anim.setEndValue(float(target))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(lambda v, bb=b: (setattr(bb, '_launch_glow', float(v)), self._apply_source_icon()))
        anim.start()
        b._launch_glow_anim = anim
        return None

    def _refresh_status(self):
        tb = getattr(self, '_taskbar', None)
        if tb is not None:
            tb.set_playing(bool((self._ui or {}).get('np_playing')))
        if not self.isVisible() or self.isMinimized():
            return None
        u = self._ui or {}
        app_cpu = u.get('app_cpu')
        if app_cpu is not None:
            txt = f'CPU - {app_cpu:.0f}%'
            if txt != getattr(self, '_last_cpu_txt', None):
                self._cpu_label.setText(txt)
                self._last_cpu_txt = txt
        ctrl = u.get('controller')
        self._lock_btn.setEnabled(ctrl is not None and hasattr(ctrl, 'set_safe_mode'))
        self._lock_btn.set_state(bool(u.get('safe')))
        running = not bool(u.get('disabled'))
        self._power_btn.set_state(running)
        game = u.get('game', None)
        speech_now = bool(u.get('speech', False))
        general_now = self._cfg.mode == 'general'
        if general_now:
            is_focused = u.get('is_focused')
            if is_focused is False:
                active_key = 'unfocused'
            elif speech_now:
                active_key = 'duck'
            else:
                active_key = 'vol'
        elif game is True:
            active_key = 'duck' if speech_now else 'vol'
        elif game is None and not bool(u.get('game_running', False)):
            active_key = 'vol'
        else:
            active_key = 'menu'
        if active_key != getattr(self, '_active_slider', None):
            self._active_slider = active_key
            names = getattr(self, '_slider_names', {})
            for k, lbl in names.items():
                if k in ('vol', 'menu', 'unfocused', 'duck'):
                    lbl.setStyleSheet(f'color: {_ACCENT};' if k == active_key else f'color: {_c("text")};')
        playing = bool(u.get('np_playing'))
        self._btn_play.setIcon(self._icon_pause if playing else self._icon_play)
        self._set_prop(self._btn_play, 'playing', playing)
        vs = self._sliders.get('vol')
        v = u.get('volume')
        if vs is not None and v is not None:
            if not vs.isSliderDown():
                iv = max(0, min(100, int(round(v * 100))))
                if iv != vs.value():
                    vs.blockSignals(True)
                    vs.setValue(iv)
                    vs.blockSignals(False)
                    vv = self._slider_vals.get('vol')
                    if vv is not None:
                        vv.setText(f'{iv}%')
        game = u.get('game', None)
        game_ok = game is not None
        game_running = bool(u.get('game_running', False))
        if self._cfg.mode == 'general':
            forza_seen = game_running
        else:
            forza_seen = game_ok or game_running
        if self._cfg.demo_mode:
            forza_seen = True
            game_ok = True
            game = True
        title = u.get('np_title', '')
        artist = u.get('np_artist', '')
        spotify_ok = bool(title)
        source_active = bool(u.get('source_active'))
        src_ok = spotify_ok or source_active
        try:
            from fh6_spotify import source_launch as _sl
            _can_launch = (not src_ok) and bool(_sl.recipe_for(self._cfg)[0])
        except Exception:
            _can_launch = False
        if getattr(self, '_spot_lbl', None) is not None:
            if src_ok:
                if getattr(self._spot_lbl, '_launching', False):
                    self._stop_launch_spinner()
            if getattr(self._spot_lbl, '_launchable', None) != _can_launch:
                self._spot_lbl._launchable = _can_launch
                self._apply_source_icon()
        if self._ui is not None:
            _mm = bool(self._ui.get('overlay_move_mode', False))
            if _mm != getattr(self, '_overlay_moving', False):
                self._set_move_mode(_mm)
        _src = self._cfg.source
        if _src == 'browser':
            src_name, on_pm, off_pm = 'Browser', self._browser_on, self._browser_off
            bpm = self._browser_icon_for(u.get('np_app', ''))
            if bpm is not None:
                on_pm, off_pm = bpm, _dim_pixmap(bpm)
        elif _src == 'applemusic':
            src_name, on_pm, off_pm = 'Apple Music', self._am_on, self._am_off
        elif _src == 'localmedia':
            src_name, on_pm, off_pm = 'Local files', self._lm_on, self._lm_off
        elif _src == 'tidal':
            src_name, on_pm, off_pm = 'TIDAL', self._tidal_on, self._tidal_off
        elif _src == 'amazonmusic':
            src_name, on_pm, off_pm = 'Amazon Music', self._amazon_on, self._amazon_off
        elif _src == 'ytmusic':
            src_name, on_pm, off_pm = 'YouTube Music', self._ytm_on, self._ytm_off
        elif _src == 'custom':
            src_name = getattr(self._cfg, 'custom_label', '') or 'Custom'
            _cip = getattr(self._cfg, 'custom_icon_path', '')
            _cpm = _exe_icon(_cip, 'C', 26).pixmap(_s(26), _s(26)) if _cip else _custom_glyph_pixmap(26)
            on_pm, off_pm = _cpm, _dim_pixmap(_cpm)
        else:
            src_name, on_pm, off_pm = 'Spotify', self._spot_on, self._spot_off
        general_mode = self._cfg.mode == 'general'
        cur_preset = self._cfg.game_preset
        if cur_preset != getattr(self, '_last_seen_preset', None):
            self._last_seen_preset = cur_preset
            try:
                self._apply_preset_visibility()
            except Exception:
                pass
        if general_mode:
            preset_icon_path = ''
            if self._cfg.game_preset == 'rocketleague':
                cand = os.path.join(_ASSETS, 'rocketleague.png')
                if os.path.exists(cand):
                    preset_icon_path = cand
            if preset_icon_path:
                pm = QPixmap(preset_icon_path).scaled(_s(26), _s(26), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._game_icon = QIcon(pm)
            elif self._cfg.general_target_process and forza_seen:
                exe_path = u.get('game_exe_path') or ''
                self._game_icon = self._game_icon_for(exe_path)
            else:
                qpm = _app_icon('', '?', 26).scaled(_s(26), _s(26), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._game_icon = QIcon(qpm)
        else:
            self._game_icon = QIcon(self._forza_on if forza_seen else self._forza_off)
        try:
            _game_can = (not forza_seen) and bool(self._game_launch_names())
        except Exception:
            _game_can = False
        if getattr(self, '_forza_lbl', None) is not None:
            if forza_seen:
                if getattr(self._forza_lbl, '_launching', False):
                    self._stop_launch_spinner(self._forza_lbl, self._apply_game_icon)
            self._forza_lbl._launchable = _game_can
        self._apply_game_icon()
        self._src_icon = QIcon(on_pm if src_ok else off_pm)
        self._apply_source_icon()
        linked = forza_seen and src_ok
        if linked and not self._link_was_linked:
            self._link_was_linked = True
            try:
                self._link_pulse.stop()
                self._link_entrance.stop()
                self._link_entrance.start()
            except Exception:
                pass
        elif not linked and self._link_was_linked:
            self._link_was_linked = False
            try:
                self._link_entrance.stop()
                self._link_pulse.stop()
                self._link_glow_fx.setBlurRadius(0.0)
            except Exception:
                pass
        if not linked:
            self._conn_lbl.setPixmap(self._link_off)

        def _pretty(name: str) -> str:
            n = (name or '').rsplit('.', 1)[0]
            low = n.lower()
            for suf in ('-win64-shipping', '-wingdk-shipping', '-win32-shipping', '-shipping', '-win64', '-win32'):
                if low.endswith(suf):
                    n = n[:-len(suf)]
                    break
            if n:
                return n[:1].upper() + n[1:]
            return 'game'

        game_label = _pretty(self._cfg.general_target_process) if general_mode else 'Forza'
        if general_mode:
            if not self._cfg.general_target_process:
                text = 'Pick a game ▾'
            elif forza_seen and src_ok:
                text = f'In game - {game_label}'
            elif not forza_seen and not src_ok:
                text = f'{game_label} & {src_name}\nnot detected'
            elif not forza_seen:
                text = f'{game_label} not running'
            else:
                text = f'{src_name} not playing'
        else:
            if game_ok and src_ok:
                text = 'Driving' if game is True else 'In menu'
            elif forza_seen and src_ok and not game_ok:
                text = 'Forza loading'
            elif not forza_seen and not src_ok:
                text = f'Forza & {src_name}\nnot detected'
            elif not forza_seen:
                text = 'Forza not detected'
            else:
                text = f'{src_name} not playing'
        if text != getattr(self, '_conn_text_last', None):
            self._conn_text_last = text
            self._conn_text.setText(text)
            self._conn_text.updateGeometry()
            QTimer.singleShot(0, self._fit_window)
        if spotify_ok:
            self._np_title.setText(title if len(title) <= 26 else title[:25] + '…')
            art = artist or _friendly_app(u.get('np_app', ''))
            self._np_artist.setText(art if len(art) <= 30 else art[:29] + '…')
        elif source_active:
            self._np_title.setText(src_name)
            self._np_artist.setText('No track info')
        else:
            self._np_title.setText('Nothing playing')
            self._np_artist.setText('')
        self._np_raw_title = title if spotify_ok else ''
        self._np_raw_artist = artist if spotify_ok else ''
        self._np_clickable = bool(spotify_ok and self._cfg.source == 'spotify')
        _np_cur = Qt.PointingHandCursor if self._np_clickable else Qt.ArrowCursor
        self._np_title.setCursor(_np_cur)
        self._np_artist.setCursor(_np_cur if self._np_clickable and self._np_raw_artist else Qt.ArrowCursor)
        self._bars.set_active(bool(u.get('np_playing', False)))
        thumb = u.get('np_thumb')
        files_default = (not thumb) and self._cfg.source == 'localmedia' and src_ok
        cover_key = (thumb, files_default)
        if cover_key != getattr(self, '_last_cover_key', None):
            self._last_cover_key = cover_key
            self._last_thumb = thumb
            if thumb:
                pm = _rounded_cover(thumb, self._cover_sz, _s(8))
            elif files_default:
                pm = _files_cover_placeholder(self._cover_sz, _s(8))
            else:
                pm = _cover_placeholder(self._cover_sz, _s(8))
            self._cover.setPixmap(pm)
        pk = getattr(self, '_overlay_pos_picker', None)
        if pk is not None:
            cx, cy = self._cfg.overlay_custom_x, self._cfg.overlay_custom_y
            if (cx, cy) != (pk._cx, pk._cy) or pk._current != self._cfg.overlay_position:
                pk._cx, pk._cy = cx, cy
                pk._current = self._cfg.overlay_position
                pk.update()
                return None
            return None
        return None

    def _on_volume(self, x):
        if self._ui is not None:
            self._ui['volume_set'] = x / 100.0
        self._cfg.full_level = x / 100.0
        self._cfg.full_level_user_set = True
        self._queue_save()
        return None

    def _on_menu(self, x):
        self._cfg.menu_level = x / 100.0
        self._queue_save()
        return None

    def _on_unfocused(self, x):
        self._cfg.unfocused_level = x / 100.0
        self._queue_save()
        return None

    def _on_fade(self, ms):
        self._cfg.volume_ramp_in = _ramp_from_ms(ms)
        self._queue_save()
        return None

    def _on_overlay(self, on):
        self._cfg.overlay_enabled = bool(on)
        self._update_overlay_subs_enabled()
        if on and self._ui is not None:
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _on_autostart(self, on):
        if on:
            _autostart.install(direct=False)
        elif _autostart.installed_mode() == 'watch':
            _autostart.uninstall()
        self._sync_startup_checks()
        return None

    def _on_lowcpu(self, on):
        self._cfg.low_cpu_mode = bool(on)
        self._queue_save()
        return None

    def _on_overlay_pos(self, key):
        if key:
            self._cfg.overlay_position = key
            self._cfg.overlay_custom_x = -1.0
            self._cfg.overlay_custom_y = -1.0
            if self._ui is not None:
                self._ui['overlay_ping'] = time.monotonic()
            self._queue_save()
            return None

    def _on_overlay_custom(self, x: float, y: float):
        """Drag-released or right-click-reset on the position picker.
        Negative values = reset to preset."""
        self._cfg.overlay_custom_x = x
        self._cfg.overlay_custom_y = y
        if self._ui is not None:
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _on_overlay_size(self, value: int):
        scale = value / 100.0
        self._cfg.overlay_scale = scale
        self._overlay_size_val.setText(f'{value}%')
        self._overlay_size_reset.setVisible(value != 100)
        if self._ui is not None:
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _on_only_cover(self, on: bool):
        self._cfg.overlay_compact = bool(on)
        if self._ui is not None:
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _on_overlay_always(self, on: bool):
        self._cfg.overlay_always_on = bool(on)
        if self._ui is not None:
            self._ui['overlay_always_on'] = bool(on)
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _on_overlay_ingame(self, on: bool):
        self._cfg.overlay_in_game_only = bool(on)
        if self._ui is not None:
            self._ui['overlay_in_game_only'] = bool(on)
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _on_overlay_drive(self, on: bool):
        self._cfg.overlay_drive_only = bool(on)
        if self._ui is not None:
            self._ui['overlay_drive_only'] = bool(on)
            self._ui['overlay_ping'] = time.monotonic()
        self._queue_save()
        return None

    def _set_move_mode(self, on: bool):
        """Set on-screen move mode + reflect it in the button label. Single source
        of truth, called by the button click AND when the overlay toggles move
        mode itself (double-click), kept in sync via the shared ui flag in
        _refresh_status."""
        self._overlay_moving = on
        if on and not self._cfg.overlay_enabled:
            self._overlay_cb.setChecked(True)
        if self._ui is not None:
            self._ui['overlay_move_mode'] = on
            self._ui['overlay_ping'] = time.monotonic()
        self._move_overlay_btn.setIcon(QIcon(_move_pixmap(self._move_sz, _ACCENT if on else _c('text_hint'))))
        return None

    def _on_move_overlay(self):
        """Toggle on-screen move mode: the real overlay becomes grabbable so the
        user drags it anywhere; drop persists custom_x/y. Click again = done."""
        self._set_move_mode(not getattr(self, '_overlay_moving', False))
        return None

    def _update_overlay_subs_enabled(self):
        """Grey out the position picker + size slider + sub-toggles when
        the overlay itself is disabled."""
        on = self._cfg.overlay_enabled
        for w in (self._only_cover_cb, self._always_on_cb, self._ingame_cb, self._drive_cb, self._overlay_pos_picker, self._overlay_size_slider, self._overlay_size_val, self._overlay_size_reset):
            w.setEnabled(on)
        return None

    def _pick_game(self):
        picked = _GamePickerDialog.pick(self, self._cfg.general_target_process)
        if picked and picked != self._cfg.general_target_process:
            self._cfg.general_target_process = picked
            self._game_name_lbl.setText(picked)
            self._queue_save()
            return None

    def _on_duck(self, x):
        self._cfg.duck_level = x / 100.0
        self._queue_save()
        return None

    def _on_sensitivity(self, x):
        self._cfg.vad_threshold = _sens_to_thresh(x)
        self._queue_save()
        return None

    def _on_ducking(self, on):
        self._cfg.ducking_enabled = bool(on)
        self._duck_row.setEnabled(bool(on))
        if hasattr(self, '_sens_row'):
            self._sens_row.setEnabled(bool(on))
        if hasattr(self, '_duckscope_cb'):
            self._duckscope_cb.setEnabled(bool(on))
        if hasattr(self, '_ownvoice_cb'):
            self._ownvoice_cb.setEnabled(bool(on) and getattr(self._cfg, 'duck_scope', 'game') == 'system')
        if hasattr(self, '_mic_row'):
            self._mic_combo.setEnabled(self._ownvoice_cb.isEnabled() and self._ownvoice_cb.isChecked())
        self._queue_save()
        return None

    def _on_duck_scope(self, on):
        self._cfg.duck_scope = 'system' if on else 'game'
        if hasattr(self, '_ownvoice_cb'):
            self._ownvoice_cb.setEnabled(self._cfg.ducking_enabled and bool(on))
        if hasattr(self, '_mic_row'):
            self._mic_combo.setEnabled(self._ownvoice_cb.isEnabled() and self._ownvoice_cb.isChecked())
        self._queue_save()
        return None

    def _on_own_voice(self, on):
        self._cfg.duck_on_own_voice = bool(on)
        if hasattr(self, '_mic_row'):
            self._mic_combo.setEnabled(self._ownvoice_cb.isEnabled() and bool(on))
        self._queue_save()
        return None

    def _on_mic_device(self, idx):
        names = getattr(self, '_mic_names', [])
        self._cfg.mic_device = names[idx - 1] if 0 < idx <= len(names) else ''
        self._queue_save()
        return None

    @staticmethod
    def _support_html(col: str, hover_url: str | None = None) -> str:
        bright = _c('accent_hi')
        kofi_c = bright if hover_url == _SUPPORT_URL else col
        disc_c = bright if hover_url == _DISCORD_URL else col
        return f'<a href="{_SUPPORT_URL}" style="color:{kofi_c}; text-decoration:none;">Support me</a> if you wanna&nbsp;&nbsp;·&nbsp;&nbsp;<a href="{_DISCORD_URL}" style="color:{disc_c}; text-decoration:none;">Join the Discord</a>'

    def _panel_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(_ui_font(16, QFont.Bold))
        _sty = lambda w=lbl: w.setStyleSheet(f"color: {_c('text')};")
        _sty()
        self._themed.append(_sty)
        return lbl

    def _media_btn(self, kind: str, slot, size: int, white: bool = False) -> QPushButton:
        b = QPushButton()
        b.setObjectName('playbtn' if white else 'mediabtn')
        b.setCursor(Qt.PointingHandCursor)
        col = _c('emph_text') if white else _c('icon')
        b.setIcon(QIcon(_media_pixmap(kind, col, size)))
        b.setIconSize(QSize(_s(size), _s(size)))
        b.clicked.connect(lambda _=False: slot())
        return b

    def _attach_press_bounce(self, btn, dip=0.9):
        """Subtle press feedback: a gentle DIP - the icon eases darker and scales
        down a hair, then springs back. ONLY the icon dips; the button frame never
        moves (other UI butts against its edges).

        The icon is rendered EXACTLY as the style draws it (via CE_PushButtonLabel,
        so size + crispness match rest - no shimmer, and an icon the style shrinks
        like the 0.92x gear is NOT stretched back up) into a 2x buffer, which is then
        offset so its glyph lands on the full-draw centre and scaled around it. The
        offset is zero on every button except a grown active tab, where
        CE_PushButtonLabel draws the glyph lower than the full draw - there it
        corrects the position so the dip can't drift.

        Dim is icon-only too (a darkened copy of the icon), so the white Play pill is
        left alone and a dark glyph goes darker, not 'brighter'."""
        import math
        from PySide6.QtCore import Qt, QRectF, QVariantAnimation, QEasingCurve
        from PySide6.QtGui import QColor, QPainter, QPixmap, QIcon
        from PySide6.QtWidgets import QStyleOptionButton, QStyle, QStylePainter

        btn._press_dim = 0.0
        btn._press_scale = 1.0

        def _pe(e, b=btn):
            opt = QStyleOptionButton()
            b.initStyleOption(opt)
            opt.state &= ~QStyle.StateFlag.State_Sunken
            d = getattr(b, '_press_dim', 0.0)
            s = getattr(b, '_press_scale', 1.0)
            if d > 0.01:
                isz = b.iconSize()
                src = QIcon(opt.icon)
                try:
                    pm = src.pixmap(isz, b.devicePixelRatioF())
                except TypeError:
                    pm = src.pixmap(isz)
                if not pm.isNull():
                    dk_pm = QPixmap(pm)
                    dp = QPainter(dk_pm)
                    dp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                    dp.fillRect(dk_pm.rect(), QColor(0, 0, 0, int(255 * d)))
                    dp.end()
                    opt.icon = QIcon(dk_pm)
            if s >= 0.999:
                QStylePainter(b).drawControl(QStyle.ControlElement.CE_PushButton, opt)
                return None
            SS = 2.0
            w, h = b.width(), b.height()
            icon = QIcon(opt.icon)
            opt.icon = QIcon()
            QStylePainter(b).drawControl(QStyle.ControlElement.CE_PushButton, opt)
            opt.icon = icon
            buf = QPixmap(max(1, int(w * SS)), max(1, int(h * SS)))
            buf.setDevicePixelRatio(SS)
            buf.fill(Qt.transparent)
            sp = QStylePainter(buf, b)
            sp.drawControl(QStyle.ControlElement.CE_PushButtonLabel, opt)
            sp.end()
            key = (w, h, b.property('active'), b.property('dull'))
            if getattr(b, '_ic_key', None) != key:
                tc = _style_icon_center(b, opt, SS)
                lc = _alpha_bbox_center(buf, SS)
                if tc is None or lc is None:
                    cr = b.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, opt, b)
                    tc = QRectF(cr).center() if cr.isValid() else QRectF(b.rect()).center()
                    lc = tc
                b._ic_tc = tc
                b._ic_off = (tc.x() - lc.x(), tc.y() - lc.y())
                b._ic_key = key
            tc = b._ic_tc
            ox, oy = b._ic_off
            p = QPainter(b)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.translate(tc.x(), tc.y())
            p.scale(s, s)
            p.translate(-tc.x(), -tc.y())
            p.drawPixmap(QRectF(ox, oy, w, h), buf, QRectF(buf.rect()))
            p.end()
            return None
        btn.paintEvent = _pe

        peak_dim = 0.3
        peak_scale = 0.07
        anim = QVariantAnimation(btn)
        anim.setDuration(190)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Linear)

        def _upd(t, b=btn, pd=peak_dim, ps=peak_scale):
            f = math.sin(float(t) * math.pi)
            b._press_dim = pd * f
            b._press_scale = 1.0 - ps * f
            b.update()
            return None
        anim.valueChanged.connect(_upd)
        anim.finished.connect(lambda b=btn: (setattr(b, '_press_dim', 0.0), setattr(b, '_press_scale', 1.0), b.update()))

        def _mp(e, b=btn, a=anim):
            a.stop()
            a.start()
            type(b).mousePressEvent(b, e)
            return None
        btn.mousePressEvent = _mp
        btn._press_anim = anim
        return None

    def _attach_pill_press(self, btn, children):
        """Whole-button press dip for the Controls pill (the 'whole button moves'
        feel). The pill has child QLabels for its text, which paint themselves on
        top of the button - so we can't just scale the paintEvent. Instead: on
        press we grab the whole pill (labels included) into a pixmap, scale THAT
        down around the centre each frame, and swallow the labels' own paints for
        the duration so they don't redraw at full size over the shrinking grab."""
        import math
        from PySide6.QtCore import QObject, QEvent, QRectF, QVariantAnimation, QEasingCurve
        from PySide6.QtGui import QPainter, QPainterPath

        btn._press_scale = 1.0
        btn._dipping = False
        btn._dip_grab = None

        class _PaintEater(QObject):
            def eventFilter(self, obj, ev):
                if getattr(btn, '_dipping', False) and ev.type() == QEvent.Type.Paint:
                    return True
                return False

        eater = _PaintEater(btn)
        for c in children:
            c.installEventFilter(eater)
        btn._pill_eater = eater

        def _pe(e, b=btn):
            if getattr(b, '_dipping', False) and b._dip_grab is not None:
                s = getattr(b, '_press_scale', 1.0)
                p = QPainter(b)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                cx, cy = b.width() / 2.0, b.height() / 2.0
                p.translate(cx, cy)
                p.scale(s, s)
                p.translate(-cx, -cy)
                rr = _s(8)
                path = QPainterPath()
                path.addRoundedRect(QRectF(b.rect()), rr, rr)
                p.setClipPath(path)
                p.drawPixmap(0, 0, b._dip_grab)
                p.end()
                return None
            self._paint_controls_pill(b, e)
            return None
        btn.paintEvent = _pe

        peak = 0.06
        anim = QVariantAnimation(btn)
        anim.setDuration(190)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Linear)

        def _upd(t, b=btn):
            b._press_scale = 1.0 - peak * math.sin(float(t) * math.pi)
            b.update()
            return None
        anim.valueChanged.connect(_upd)

        def _fin(b=btn):
            b._dipping = False
            b._press_scale = 1.0
            b._dip_grab = None
            for c in children:
                c.update()
            b.update()
            return None
        anim.finished.connect(_fin)

        def _mp(e, b=btn, a=anim):
            a.stop()
            b._dipping = False
            b._dip_grab = b.grab()
            b._press_scale = 1.0
            b._dipping = True
            a.start()
            type(b).mousePressEvent(b, e)
            return None
        btn.mousePressEvent = _mp
        btn._pill_anim = anim
        return None

    def _paint_src_caret(self, b, e, side, angle_attr):
        """Game picker paint: button base (native press-shift killed) + the chevron
        rotated by `angle_attr` (0 closed, 180 open). Dulled until hover."""
        from PySide6.QtWidgets import QStyleOptionButton, QStyle, QStylePainter
        opt = QStyleOptionButton()
        b.initStyleOption(opt)
        opt.state &= ~QStyle.StateFlag.State_Sunken
        QStylePainter(b).drawControl(QStyle.ControlElement.CE_PushButton, opt)
        pm = getattr(self, '_caret_pm', None)
        if pm is None or pm.isNull():
            return None
        half_w, half_h = pm.width() / 2.0, pm.height() / 2.0
        cx = b.width() - _s(7) - half_w if side == 'right' else _s(7) + half_w
        cy = b.height() / 2.0
        p = QPainter(b)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        _rest = 0.65 if _active_theme() == 'light' else 0.45
        p.setOpacity(1.0 if b.underMouse() else _rest)
        p.translate(cx, cy)
        p.rotate(getattr(self, angle_attr, 0.0))
        p.drawPixmap(int(-half_w), int(-half_h), pm)
        p.end()
        return None

    def _paint_caret_btn(self, b, e, angle_attr, side_attr='_src_hover_side'):
        """Standalone dropdown-caret button (a picker's chevron): button base with
        the native press-shift killed (so it never drifts on click) + the chevron
        CENTERED, rotated by angle_attr (0 closed, 180 open), dulled until the pill
        (side_attr) is hovered or the menu is open."""
        from PySide6.QtWidgets import QStyleOptionButton, QStyle, QStylePainter
        opt = QStyleOptionButton()
        b.initStyleOption(opt)
        opt.state &= ~QStyle.StateFlag.State_Sunken
        QStylePainter(b).drawControl(QStyle.ControlElement.CE_PushButton, opt)
        pm = getattr(self, '_caret_pm', None)
        if pm is None or pm.isNull():
            return None
        half_w, half_h = pm.width() / 2.0, pm.height() / 2.0
        p = QPainter(b)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        _rest = 0.65 if _active_theme() == 'light' else 0.45
        _lit = b.underMouse() or b.property('menuopen') == 'true' or getattr(self, side_attr, None) is not None
        p.setOpacity(1.0 if _lit else _rest)
        from PySide6.QtCore import QPointF
        ctr = getattr(self, '_caret_center', None)
        if ctr is None:
            ctr = _alpha_bbox_center(pm, 1.0) or QPointF(half_w, half_h)
            self._caret_center = ctr
        p.translate(b.width() / 2.0, b.height() / 2.0)
        p.rotate(getattr(self, angle_attr, 0.0))
        p.drawPixmap(QPointF(-ctr.x(), -ctr.y()), pm)
        p.end()
        return None

    def _rotate_caret(self, angle_attr, btn, to_angle):
        """Animate a picker chevron's rotation (smooth + slightly bouncy) toward
        to_angle (0 = down/closed, 180 = up/open)."""
        from PySide6.QtCore import QVariantAnimation, QEasingCurve
        prev = getattr(self, angle_attr + '_anim', None)
        if prev is not None:
            prev.stop()
        anim = QVariantAnimation(self)
        anim.setDuration(300)
        anim.setStartValue(float(getattr(self, angle_attr, 0.0)))
        anim.setEndValue(float(to_angle))
        anim.setEasingCurve(QEasingCurve.OutBack)
        anim.valueChanged.connect(lambda v, b=btn, a=angle_attr: (setattr(self, a, float(v)), b.update()))
        anim.start()
        setattr(self, angle_attr + '_anim', anim)
        return None

    def _set_caret_open(self, on, caret_attr='_spot_caret', box_attr='_src_box'):
        """Keep a picker caret's segment highlighted while its menu is open."""
        c = getattr(self, caret_attr, None)
        if c is None:
            return None
        c.setProperty('menuopen', 'true' if on else 'false')
        box = getattr(self, box_attr, None)
        if box is not None:
            box.update()
        c.update()
        return None

    def _set_split_side(self, box, lbl, caret, side_attr, side):
        """Which segment the cursor is over (None|'icon'|'caret') for the pill named
        by side_attr. Both halves get a base fill, the hovered one goes brighter."""
        if getattr(self, side_attr, None) == side:
            return None
        setattr(self, side_attr, side)
        if box is not None:
            box.update()
        if caret is not None:
            caret.update()
            return None

    def _split_recheck(self, box, lbl, caret, side_attr):
        """On leaving a segment, re-derive the hovered side from the cursor (so
        moving icon<->caret doesn't flicker the pill off)."""
        from PySide6.QtGui import QCursor
        gp = QCursor.pos()
        if lbl is not None and lbl.rect().contains(lbl.mapFromGlobal(gp)):
            self._set_split_side(box, lbl, caret, side_attr, 'icon')
            return None
        if caret is not None and caret.rect().contains(caret.mapFromGlobal(gp)):
            self._set_split_side(box, lbl, caret, side_attr, 'caret')
            return None
        self._set_split_side(box, lbl, caret, side_attr, None)
        return None

    def _paint_split_box(self, box, lbl, caret, side_attr):
        """Paint a split pill: two halves with rounded OUTER corners and SQUARE
        inner corners, split by the negative-space gap. Both halves light when the
        pill is hovered (hovered side brighter); the caret half also lights while
        its menu is open."""
        from PySide6.QtGui import QPainter, QPainterPath, QColor
        from PySide6.QtCore import QRectF
        if box is None or lbl is None or caret is None:
            return None
        side = getattr(self, side_attr, None)
        menuopen = caret.property('menuopen') == 'true'
        if side is None and not menuopen:
            return None
        base = QColor(_c('surface_hi'))
        bright = QColor('#404040' if _active_theme() == 'contrast' else _c('border_hi'))
        r = float(_s(8))

        def _half(rect, left):
            x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
            path = QPainterPath()
            if left:
                path.moveTo(x1, y0)
                path.lineTo(x0 + r, y0)
                path.quadTo(x0, y0, x0, y0 + r)
                path.lineTo(x0, y1 - r)
                path.quadTo(x0, y1, x0 + r, y1)
                path.lineTo(x1, y1)
            else:
                path.moveTo(x0, y0)
                path.lineTo(x1 - r, y0)
                path.quadTo(x1, y0, x1, y0 + r)
                path.lineTo(x1, y1 - r)
                path.quadTo(x1, y1, x1 - r, y1)
                path.lineTo(x0, y1)
            path.closeSubpath()
            return path

        gap = float(_s(2))
        ig = QRectF(lbl.geometry())
        cg = QRectF(caret.geometry())
        if ig.left() <= cg.left():
            ig.setRight(ig.right() - gap / 2.0)
            icon_path = _half(ig, True)
            cg.setLeft(cg.left() + gap / 2.0)
            caret_path = _half(cg, False)
        else:
            cg.setRight(cg.right() - gap / 2.0)
            caret_path = _half(cg, True)
            ig.setLeft(ig.left() + gap / 2.0)
            icon_path = _half(ig, False)
        p = QPainter(box)
        p.setRenderHint(QPainter.Antialiasing)
        if side is not None:
            p.fillPath(icon_path, bright if side == 'icon' else base)
            p.fillPath(caret_path, bright if side == 'caret' else base)
        else:
            p.fillPath(caret_path, bright)
        p.end()
        return None

    def _set_src_side(self, side):
        self._set_split_side(getattr(self, '_src_box', None), getattr(self, '_spot_lbl', None), getattr(self, '_spot_caret', None), '_src_hover_side', side)
        return None

    def _src_side_recheck(self):
        self._split_recheck(getattr(self, '_src_box', None), getattr(self, '_spot_lbl', None), getattr(self, '_spot_caret', None), '_src_hover_side')
        return None

    def _paint_src_box(self, e):
        self._paint_split_box(getattr(self, '_src_box', None), getattr(self, '_spot_lbl', None), getattr(self, '_spot_caret', None), '_src_hover_side')
        return None

    def _set_game_side(self, side):
        self._set_split_side(getattr(self, '_game_box', None), getattr(self, '_forza_lbl', None), getattr(self, '_game_caret', None), '_game_hover_side', side)
        return None

    def _game_side_recheck(self):
        self._split_recheck(getattr(self, '_game_box', None), getattr(self, '_forza_lbl', None), getattr(self, '_game_caret', None), '_game_hover_side')
        return None

    def _paint_game_box(self, e):
        self._paint_split_box(getattr(self, '_game_box', None), getattr(self, '_forza_lbl', None), getattr(self, '_game_caret', None), '_game_hover_side')
        return None

    @staticmethod
    def _set_prop(btn, name: str, on: bool):
        want = 'true' if on else 'false'
        if btn.property(name) == want:
            return None
        btn.setProperty(name, want)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        return None

    def _toggle_lock(self):
        ctrl = (self._ui or {}).get('controller')
        if ctrl is not None:
            if hasattr(ctrl, 'set_safe_mode'):
                ctrl.set_safe_mode(not ctrl.safe_mode)
                self._refresh_status()
                return None

    def _toggle_disabled(self):
        if self._ui is None:
            return None
        self._ui['disabled'] = not self._ui.get('disabled', False)
        self._refresh_status()
        return None

    def _browser_icon_for(self, appid: str):
        """Real browser logo for the Browser source: prefer a bundled asset
        (assets/browser_<name>.png), else the running browser's own exe icon, else
        None (caller keeps the generic globe). Cached per browser name; only
        successes are cached so it retries until the browser is found running."""
        friendly = _friendly_app(appid)
        if not friendly or friendly in ('Spotify', 'Apple Music', 'Media Player', 'VLC'):
            return None
        cache = getattr(self, '_browser_icon_cache', None)
        if cache is None:
            self._browser_icon_cache = cache = {}
        if friendly in cache:
            return cache[friendly]
        pm = None
        asset = os.path.join(_ASSETS, 'browser_' + friendly.lower().replace(' ', '') + '.png')
        if os.path.exists(asset):
            cand = QPixmap(asset)
            if not cand.isNull():
                pm = cand.scaled(_s(28), _s(28), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if pm is None:
            exe = _find_browser_exe(friendly)
            if exe:
                try:
                    from PySide6.QtWidgets import QFileIconProvider
                    from PySide6.QtCore import QFileInfo
                    ic = QFileIconProvider().icon(QFileInfo(exe))
                    if not ic.isNull():
                        cand = ic.pixmap(_s(28), _s(28))
                        if not cand.isNull():
                            pm = cand
                except Exception:
                    pm = None
        if pm is not None:
            cache[friendly] = pm
        return pm

    def _game_icon_for(self, exe_path: str) -> QIcon:
        """Extract the game's own icon from its .exe via Qt's QFileIconProvider.
        Cached per-path so we don't hit the disk every UI refresh. Falls back
        to a generic gamepad glyph when the exe isn't found / has no icon."""
        cache = getattr(self, '_game_icon_cache', None)
        if cache is None:
            self._game_icon_cache = cache = {}
        if exe_path in cache:
            return cache[exe_path]
        icon = None
        if exe_path and os.path.exists(exe_path):
            try:
                from PySide6.QtWidgets import QFileIconProvider
                from PySide6.QtCore import QFileInfo
                provider = QFileIconProvider()
                ext = provider.icon(QFileInfo(exe_path))
                if not ext.isNull():
                    icon = ext
            except Exception:
                icon = None
        if icon is None:
            icon = _dev_icon('xbox', 28)
        cache[exe_path] = icon
        return icon

    def _close_open_pickers(self):
        """Enforce one picker at a time: close the game popup AND the source menu if
        either is showing, so opening one (or clicking another button) closes the
        other. Belt-and-suspenders alongside the game popup's outside-click filter."""
        p = getattr(self, '_game_popup', None)
        if p is not None:
            try:
                p.close()
            except Exception:
                pass
            self._game_popup = None
        m = getattr(self, '_src_menu_cached', None)
        if m is not None:
            try:
                if m.isVisible():
                    m.close()
                    return None
            except Exception:
                return None

    def _game_menu(self):
        """Popup off the game icon: switch between curated game presets.
        Uses _IconPopup so each row shows a properly-scaled game icon
        (QMenu's icon size is locked to ~16 px)."""
        if time.monotonic() - self._game_menu_closed_at < 0.25:
            return None
        self._close_open_pickers()
        from fh6_spotify import game_presets as _gp
        current = self._cfg.game_preset
        sz = _s(22)
        items = []
        for key in _gp.GAME_PRESETS:
            label = _gp.label_for(key)
            if key == 'other':
                cur_target = self._cfg.general_target_process
                label = f'Other game ({cur_target})' if cur_target and current == 'other' else 'Other game…'
            icon_path = ''
            if key == 'forza':
                icon_path = _FORZA
            elif key == 'rocketleague':
                cand = os.path.join(_ASSETS, 'rocketleague.png')
                if os.path.exists(cand):
                    icon_path = cand
            if key == 'forza':
                pm = _forza_pixmap(22) or QPixmap(_FORZA).scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            elif icon_path and os.path.exists(icon_path):
                pm = QPixmap(icon_path).scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                letter = (label[:1] if key != 'other' else '?').upper()
                pm = _app_icon('', letter, 22)
                pm = pm.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            items.append((key, label, pm))

        popup = _IconPopup(self, items, current, sz)
        self._game_popup = popup

        def _on_pick(key: str) -> None:
            if key == 'other':
                self._set_other_game()
                return None
            self._apply_game_preset(key)
            return None
        popup.selected.connect(_on_pick)

        sep = QFrame()
        sep.setObjectName('vline')
        sep.setFixedHeight(1)
        sep.setStyleSheet(f'background: {_c("border")}; margin: {_s(4)}px 0;')
        popup.layout().addWidget(sep)

        auto_cb = QCheckBox('Auto-detect game')
        auto_cb.setChecked(self._cfg.auto_detect_game)
        auto_cb.setCursor(Qt.PointingHandCursor)
        auto_cb.setFont(_ui_font(14))
        auto_cb.setStyleSheet(f'QCheckBox {{ color: {_c("text")}; padding: {_s(8)}px {_s(12)}px; }}')

        def _on_auto(checked: bool) -> None:
            self._cfg.auto_detect_game = bool(checked)
            self._queue_save()
            return None
        auto_cb.toggled.connect(_on_auto)
        popup.layout().addWidget(auto_cb)

        popup.closed.connect(lambda: (setattr(self, '_game_menu_closed_at', time.monotonic()), setattr(self, '_game_popup', None), self._set_caret_open(False, '_game_caret', '_game_box'), self._game_side_recheck(), self._rotate_caret('_game_caret_angle', self._game_caret, 0.0)))

        self._set_caret_open(True, '_game_caret', '_game_box')
        self._rotate_caret('_game_caret_angle', self._game_caret, 180.0)
        popup.popup_at(self._game_box.mapToGlobal(self._game_box.rect().bottomLeft()))
        return None

    def _apply_game_preset(self, key: str) -> None:
        """User picked a preset from the chevron menu (or auto-detect did).
        Apply its defaults to cfg + persist + refresh the panel visibility
        so irrelevant Mixer/Extras rows hide for the new preset."""
        from fh6_spotify import game_presets as _gp
        if key == self._cfg.game_preset:
            return None
        _gp.apply_preset(self._cfg, key)
        if hasattr(self, '_game_row'):
            self._game_row.setVisible(key == 'other')
        self._queue_save()
        self._apply_preset_visibility()
        self._refresh_status()
        return None

    def _toggle_auto_detect_game(self) -> None:
        self._cfg.auto_detect_game = not self._cfg.auto_detect_game
        self._queue_save()
        return None

    def _set_other_game(self) -> None:
        picked = _GamePickerDialog.pick(self, self._cfg.general_target_process)
        if not picked:
            return None
        from fh6_spotify import game_presets as _gp
        matched = None
        for key, p in _gp.GAME_PRESETS.items():
            if p.get('exe', '').lower() == picked.lower():
                matched = key
                break
        if matched and matched != 'other':
            self._apply_game_preset(matched)
            return None
        self._cfg.general_target_process = picked
        _gp.apply_preset(self._cfg, 'other')
        self._cfg.general_target_process = picked
        if hasattr(self, '_game_name_lbl'):
            self._game_name_lbl.setText(picked)
        if hasattr(self, '_game_row'):
            self._game_row.setVisible(True)
        self._queue_save()
        self._apply_preset_visibility()
        self._refresh_status()
        return None

    def _toggle_mixer_adv(self) -> None:
        """Expand / collapse the Mixer's Advanced section."""
        self._mixer_adv_open = not self._mixer_adv_open
        self._refresh_adv_rows()
        QTimer.singleShot(0, self._fit_window)
        QTimer.singleShot(60, self._fit_window)
        return None

    def _refresh_adv_rows(self) -> None:
        """Show each Advanced row only when the section is open AND the current
        preset surfaces that control. Hides the Advanced button entirely when
        the preset has no advanced controls (e.g. Rocket League)."""
        from fh6_spotify import game_presets as _gp
        key = self._cfg.game_preset
        any_avail = False
        for ctrl, row in getattr(self, '_adv_rows', []):
            avail = _gp.show_control(key, ctrl)
            any_avail = any_avail or avail
            row.setVisible(self._mixer_adv_open and avail)
        self._mixer_adv_btn.setVisible(any_avail)
        if not any_avail:
            self._mixer_adv_open = False
        self._mixer_adv_btn.update()
        return None

    def _apply_preset_visibility(self) -> None:
        """Hide / show Mixer + Extras controls based on the current preset's
        `show` dict. Called after the panel is built and on preset switch.
        Safe to call when widgets haven't been built yet - skips missing."""
        from fh6_spotify import game_presets as _gp
        key = self._cfg.game_preset
        if hasattr(self, '_sliders'):
            unfoc_slider = self._sliders.get('unfocused')
            if unfoc_slider is not None:
                unfoc_slider.parent().setVisible(_gp.show_control(key, 'unfocused_volume'))
            menu_slider = self._sliders.get('menu')
            if menu_slider is not None:
                menu_slider.parent().setVisible(_gp.show_control(key, 'menu_volume'))
        if hasattr(self, '_adv_rows'):
            self._refresh_adv_rows()
        QTimer.singleShot(0, self._fit_window)
        return None

    def _source_menu(self):
        """Popup off the source icon: choose the music app. The QMenu is built
        ONCE and reused (rebuilt only when the source or custom icon changes) -
        rebuilding the whole menu + proxy style + submenu on every open was extra
        cold work that showed as a first-open stall after the app sat idle (the OS
        trims an idle app's working set; less to fault back in = snappier)."""
        if time.monotonic() - self._spot_menu_closed_at < 0.25:
            return None
        self._close_open_pickers()
        menu = self._source_menu_obj()
        menu.exec(self._spot_lbl.mapToGlobal(self._spot_lbl.rect().bottomLeft()))
        return None

    def _source_menu_obj(self):
        """Build-or-reuse the source picker QMenu. Cached by (source, custom icon)
        so the tick and the custom entry's icon stay correct; every other open
        reuses the live object instead of reallocating menu + actions + style."""
        sig = (self._cfg.source, getattr(self._cfg, 'custom_icon_path', ''))
        cached = getattr(self, '_src_menu_cached', None)
        if cached is not None and getattr(self, '_src_menu_sig', None) == sig:
            return cached
        sz = _s(22)
        _cache = getattr(self, '_src_menu_pm', None)
        if _cache is None:
            def _scaled(path, fallback):
                if os.path.exists(path):
                    return QPixmap(path).scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                return fallback.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)

            spot_pm = QPixmap(_SPOTIFY).scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation) if os.path.exists(_SPOTIFY) else self._spot_on
            browser_pm = _tinted(os.path.join(_ASSETS, 'browser_white.png'), _c('icon'), 22) or _globe_pixmap(22)
            am_pm = _scaled(os.path.join(_ASSETS, 'applemusic.png'), _app_icon('', 'M', 22))
            lm_pm = _scaled(os.path.join(_ASSETS, 'localmedia.png'), _folder_pixmap(22))
            _td = os.path.join(_ASSETS, 'tidal.png')
            td_pm = _zoom_icon(_td, sz, 1.32) if os.path.exists(_td) else _app_icon('', 'T', 22).scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            amz_pm = _scaled(os.path.join(_ASSETS, 'amazonmusic.png'), _app_icon('', 'A', 22))
            _ytm = os.path.join(_ASSETS, 'ytmusic.png')
            ytm_pm = _zoom_icon(_ytm, sz, 1.18) if os.path.exists(_ytm) else _app_icon('', 'Y', 22).scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _cache = self._src_menu_pm = {'spot': spot_pm, 'browser': browser_pm, 'am': am_pm, 'lm': lm_pm, 'td': td_pm, 'amz': amz_pm, 'ytm': ytm_pm}
        spot_pm = _cache['spot']
        browser_pm = _cache['browser']
        am_pm = _cache['am']
        lm_pm = _cache['lm']
        td_pm = _cache['td']
        amz_pm = _cache['amz']
        ytm_pm = _cache['ytm']

        items = [
            ('spotify', 'Spotify', spot_pm),
            ('applemusic', 'Apple Music', am_pm),
            ('browser', 'Browser (YouTube etc.)', browser_pm),
        ]
        more_items = [
            ('localmedia', 'Local files (Media Player)', lm_pm),
            ('tidal', 'TIDAL', td_pm),
            ('amazonmusic', 'Amazon Music', amz_pm),
            ('ytmusic', 'YouTube Music (desktop app)', ytm_pm),
        ]
        menu = QMenu(self)
        _round_menu(menu)
        menu.setFont(_ui_font(14))
        menu.setCursor(Qt.PointingHandCursor)
        self._src_menu_style = _BigMenuIconStyle(sz)
        menu.setStyle(self._src_menu_style)
        cur = self._cfg.source
        for key, label, pm in items:
            act = menu.addAction(QIcon(pm), label + ('    ✓' if key == cur else ''))
            act.triggered.connect(lambda _=False, k=key: self._set_source(k))
        _cust_path = getattr(self._cfg, 'custom_icon_path', '')
        _cust_icon = _exe_icon(_cust_path, 'C', 22) if _cust_path else QIcon(_custom_glyph_pixmap(22))
        cust_act = menu.addAction(_cust_icon, 'Custom…' + ('    ✓' if cur == 'custom' else ''))
        cust_act.triggered.connect(lambda _=False: self._pick_custom_source())
        more = _FadeMenu('More', menu)
        _round_menu(more)
        more.setFont(_ui_font(14))
        more.setCursor(Qt.PointingHandCursor)
        more.setStyle(self._src_menu_style)
        more.menuAction().setIcon(QIcon(_dots_tile_pixmap(22)))
        for key, label, pm in more_items:
            act = more.addAction(QIcon(pm), label + ('    ✓' if key == cur else ''))
            act.triggered.connect(lambda _=False, k=key: self._set_source(k))
        menu.addMenu(more)
        menu.aboutToShow.connect(lambda: (self._set_caret_open(True), self._rotate_caret('_src_caret_angle', self._spot_caret, 180.0)))
        menu.aboutToHide.connect(lambda: (setattr(self, '_spot_menu_closed_at', time.monotonic()), self._set_caret_open(False), self._src_side_recheck(), self._rotate_caret('_src_caret_angle', self._spot_caret, 0.0)))
        self._src_menu_cached = menu
        self._src_menu_sig = sig
        return menu

    def _set_source(self, key: str):
        if key == self._cfg.source:
            return None
        self._cfg.source = key
        self._queue_save()
        self._refresh_status()
        return None

    def _launch_source(self):
        """Open the configured source app (the source menu's 'Open X' entry)."""
        from fh6_spotify import source_launch as _sl
        r = _sl.launch_source(self._cfg)
        msg = {'running': f'{r.display} is already open', 'launched': f'Opening {r.display}…', 'unsupported': f'Open {r.display} manually to start playback', 'not_found': f"Couldn't find {r.display}. Open it manually."}.get(r.status, '')
        if msg:
            self._source_toast(msg)
            return None

    def _source_toast(self, text: str):
        """Brief tray balloon for source-launch feedback (no modal interrupt)."""
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon
            seg = getattr(QApplication.instance(), '_segue', None)
            tray = seg.get('tray') if seg else None
            if tray is not None:
                tray.showMessage('Segue', text, QSystemTrayIcon.MessageIcon.Information, 3000)
                return None
        except Exception:
            return None

    def _pick_custom_source(self):
        from fh6_spotify import source_picker
        exclude = source_picker.builtin_source_exes(self._cfg)
        apps = source_picker.list_audio_apps(exclude=exclude)
        dlg = _CustomSourceDialog(self, apps, exclude)
        if dlg.exec() != QDialog.Accepted or dlg.picked is None:
            return None
        r = source_picker.resolve_pick(dlg.picked, source_picker.list_smtc_app_ids())
        self._cfg.custom_process_names = r['custom_process_names']
        self._cfg.custom_smtc_match = r['custom_smtc_match']
        self._cfg.custom_label = r['custom_label']
        self._cfg.custom_icon_path = r.get('custom_icon_path', '')
        self._cfg.source = 'custom'
        self._queue_save()
        self._refresh_status()
        return None

    def _queue_save(self):
        self._save_timer.start()
        return None

    def _save(self):
        try:
            self._cfg.save(self._path)
            return None
        except OSError as exc:
            print(f'save failed: {exc}')
            return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_c('bg')))
        self._paint_tab_join(p)
        self._paint_brand(p)
        return None

    def _paint_bottom_band(self, p):
        """Light mode: a dark gradient at the very bottom so the (now white) SEGUE
        stamp pops against it. Starts BELOW the footer row so its text stays on
        the light bg. No-op on dark/HC (the stamp already reads on the dark bg)."""
        if _active_theme() != 'light':
            return None
        from PySide6.QtGui import QLinearGradient
        h, w = self.height(), self.width()
        band = _s(92)
        g = QLinearGradient(0, h - band, 0, h)
        g.setColorAt(0.0, QColor(44, 44, 42, 0))
        g.setColorAt(1.0, QColor(44, 44, 42, 95))
        p.fillRect(0, h - band, w, band, g)
        return None

    def _paint_tab_join(self, p):
        """Smooth the inner corners where the active tab meets its panel with a
        concave fillet (the panel colour), so the tab flows into the rounded panel
        instead of meeting it at a hard 90 degrees."""
        if self._active_tab is None:
            return None
        btn = self._tab_mixer if self._active_tab == 'mixer' else self._tab_extras
        panel = self._card_for(self._active_tab)
        if not panel.isVisible():
            return None
        ptl = panel.mapTo(self, QPoint(0, 0))
        ptop, pleft, pright = ptl.y(), ptl.x(), ptl.x() + panel.width()
        btl = btn.mapTo(self, QPoint(0, 0))
        tx0, tx1 = btl.x(), btl.x() + btn.width()
        rf = _s(10)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_c('panel')))
        if tx0 - pleft > _s(2):
            path = QPainterPath()
            path.moveTo(tx0, ptop)
            path.lineTo(tx0, ptop - rf)
            path.arcTo(tx0 - 2 * rf, ptop - 2 * rf, 2 * rf, 2 * rf, 0, -90)
            path.closeSubpath()
            p.drawPath(path)
        if pright - tx1 > _s(2):
            path = QPainterPath()
            path.moveTo(tx1, ptop)
            path.lineTo(tx1, ptop - rf)
            path.arcTo(tx1, ptop - 2 * rf, 2 * rf, 2 * rf, 180, 90)
            path.closeSubpath()
            p.drawPath(path)
        if _active_theme() == 'light':
            pbottom = ptop + panel.height()
            ttop = btl.y()
            tr = _s(8)
            pr = _s(8)
            tl_sharp = panel.objectName() == 'tabpanelL'
            lf = tx0 - pleft > _s(2)
            rfp = pright - tx1 > _s(2)
            o = QPainterPath()
            o.moveTo(tx0 + tr, ttop)
            o.lineTo(tx1 - tr, ttop)
            o.arcTo(tx1 - 2 * tr, ttop, 2 * tr, 2 * tr, 90, -90)
            o.lineTo(tx1, ptop - rf)
            if rfp:
                o.arcTo(tx1, ptop - 2 * rf, 2 * rf, 2 * rf, 180, 90)
            o.lineTo(pright - pr, ptop)
            o.arcTo(pright - 2 * pr, ptop, 2 * pr, 2 * pr, 90, -90)
            o.lineTo(pright, pbottom - pr)
            o.arcTo(pright - 2 * pr, pbottom - 2 * pr, 2 * pr, 2 * pr, 0, -90)
            o.lineTo(pleft + pr, pbottom)
            o.arcTo(pleft, pbottom - 2 * pr, 2 * pr, 2 * pr, 270, -90)
            if lf:
                o.lineTo(pleft, ptop + (0 if tl_sharp else pr))
                if not tl_sharp:
                    o.arcTo(pleft, ptop, 2 * pr, 2 * pr, 180, -90)
                o.lineTo(tx0 - rf, ptop)
                o.arcTo(tx0 - 2 * rf, ptop - 2 * rf, 2 * rf, 2 * rf, 270, 90)
                o.lineTo(tx0, ttop + tr)
                o.arcTo(tx0, ttop, 2 * tr, 2 * tr, 180, -90)
            else:
                o.lineTo(tx0, ttop + tr)
                o.arcTo(tx0, ttop, 2 * tr, 2 * tr, 180, -90)
            o.closeSubpath()
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(_c('border')), 1.0))
            p.drawPath(o)
            return None
        return None

    def _paint_brand(self, p):
        """Big SEGUE + logo overlay bled past the bottom edge - dulled, cropped
        like a brand stamp. Rendered to a cached pixmap (Monument layout is
        slow); each repaint just blits it, so the tab animation stays smooth.
        The stamp scales WITH the window width (a wide-stretched window kept
        the old fixed-size stamp looking tiny), re-rendered only when the
        width crosses a bucket."""
        pm, baseline = self._brand_overlay(self.width())
        if pm is None:
            return None
        x = int((self.width() - pm.width()) / 2)
        y = self.height() + _s(11) - baseline
        p.drawPixmap(x, y, pm)
        return None

    def _brand_overlay(self, win_w: int = 0):
        """(pixmap, baseline-from-top) for the Segue brand stamp at the bottom of
        the UI: the shipped wordmark+arrow lockup scaled to ~88% of the window
        width (capped so a wide window keeps it inset, a narrow one shrinks it),
        with a vertical alpha gradient baked in so it fades into the bottom edge.
        Cached by EXACT width so a resize scales it smoothly (the old ~40px
        bucket made it jump in steps); re-rendering is just an image scale +
        gradient now, cheap enough to do per distinct width. Non-resize repaints
        at the same width still hit the cache."""
        bucket = int(win_w) if win_w else 0
        cached = getattr(self, '_brand_pm', None)
        if cached is not None and getattr(self, '_brand_bucket', None) == bucket:
            return (cached, self._brand_baseline)
        src = self._brand_logo
        if src is None or src.isNull():
            return (None, 0)
        target = int(min(win_w * 0.97, _s(700))) if win_w else _s(420)
        w = max(1, target)
        scaled = src.scaledToWidth(w, Qt.SmoothTransformation)
        if _active_theme() == 'light':
            scaled = _recolor_pm(scaled, '#9a9a98')
        h = max(1, scaled.height())
        pm = QPixmap(w, h)
        pm.fill(QColor(0, 0, 0, 0))
        pp = QPainter(pm)
        pp.setRenderHint(QPainter.SmoothPixmapTransform)
        pp.drawPixmap(0, 0, scaled)
        _mid_a, _bot_a = (60, 190) if _active_theme() == 'light' else (30, 130)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(0.45, QColor(0, 0, 0, _mid_a))
        grad.setColorAt(1.0, QColor(0, 0, 0, _bot_a))
        pp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        pp.fillRect(pm.rect(), grad)
        pp.end()
        baseline = int(h * 0.86)
        self._brand_pm = pm
        self._brand_bucket = bucket
        self._brand_baseline = baseline
        return (pm, baseline)


def main():
    from fh6_spotify.app import main as app_main
    app_main()


if __name__ == '__main__':
    main()
