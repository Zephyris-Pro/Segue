"""Forzify now-playing overlay (PySide6) — true per-pixel transparency.

Frameless, always-on-top, click-through, center-left. Bordered circular cover
with play/pause + lock badges on the art, anti-aliased title/artist + volume
bar. Fades in on change, fades out when idle. Muted (menu) -> cover + mute icon.

Qt runs on the main thread; the radio loop runs in a worker thread and feeds
state via get_state(). Media (cover/title/state) comes from MediaWatcher.
"""
import ctypes
from ctypes import wintypes
import os
import threading
import time
import urllib.request
_CDBG = bool(os.environ.get('SEGUE_CAROUSEL_DBG'))
_CDBG_PATH = os.path.join(os.path.dirname(__file__), '..', 'scripts', '.carousel_dbg.log')


def _clog(msg):
    if not _CDBG:
        return None
    try:
        with open(_CDBG_PATH, 'a', encoding='utf-8') as f:
            f.write('{:.3f} {}\n'.format(time.time(), msg))
    except Exception:
        return None


from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget
from PIL import Image, ImageFilter
_W, _H = (352, 132)
_COVER = 104
_PAD = 16
_BADGE_R = 19
_CR = 18
_LINGER_S = 3.5
_FONT = 'Inter'
_WHITE = QColor(255, 255, 255)
_DIM = QColor(222, 222, 230)
_TILE = 60
_TILE_GAP = 12
_TILE_PITCH = _TILE + _TILE_GAP
_TILE_CR = 10
_N_NEXT, _N_PREV = 2, 2
_SKIP_WIN = 3.0
_GOTO_SETTLE = 0.45


def _overlay_xy(position: str, screen_w: int, screen_h: int, w: int, h: int, margin: int=64, custom_x: float=-1.0, custom_y: float=-1.0) -> tuple[int, int]:
    if 0.0 <= custom_x <= 1.0 and 0.0 <= custom_y <= 1.0:
        x = int(custom_x * (screen_w - w))
        y = int(custom_y * (screen_h - h))
        x = max(0, min(screen_w - w, x))
        y = max(0, min(screen_h - h, y))
        return (x, y)
    if position.endswith('_right'):
        x = screen_w - w - margin
    elif position.endswith('_center'):
        x = (screen_w - w) // 2
    else:
        x = margin
    if position.startswith('top_'):
        y = margin
        return (x, y)
    if position.startswith('bottom_'):
        y = screen_h - h - margin
        return (x, y)
    y = (screen_h - h) // 2
    return (x, y)


_SNAP_MARGIN = 64
_SNAP_THRESH = 28


class _SnapGuides(QWidget):
    """Fullscreen click-through sheet shown while Shift-dragging the overlay:
    draws alignment guide LINES (Figma-style) for the axis you're snapped to -
    edge lines at the padded margins, centerlines through the screen middle.
    Purely visual - the magnetism lives in the overlay's mouseMoveEvent."""

    def __init__(self, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self.vline = None
        self.hline = None
        self.show()

    def set_lines(self, vx, hy) -> None:
        if vx != self.vline or hy != self.hline:
            self.vline = vx
            self.hline = hy
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(190, 190, 190, 170), 2, Qt.DashLine)
        p.setPen(pen)
        if self.vline is not None:
            p.drawLine(int(self.vline), 0, int(self.vline), self.height())
        if self.hline is not None:
            p.drawLine(0, int(self.hline), self.width(), int(self.hline))


class _MoveHint(QWidget):
    """Hint shown while move mode is active: a drawn Shift keycap (with the
    ⇧ outline arrow) + "to snap". Click-through, bottom-center of the
    overlay's screen (above the taskbar); place() follows screen changes."""
    _KEY = 'Shift'
    _TEXT = 'to snap'

    def __init__(self, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._fkey = QFont(_FONT, 19)
        self._fkey.setBold(True)
        self._ftxt = QFont(_FONT, 19)
        fmt, fmk = (QFontMetrics(self._fkey), QFontMetrics(self._ftxt))
        self._arrow_w = 22
        self._key_w = self._arrow_w + 10 + fmk.horizontalAdvance(self._KEY) + 36
        self._key_h = fmk.height() + 18
        gap = 14
        pad = 20
        w = pad + self._key_w + gap + fmt.horizontalAdvance(self._TEXT) + pad
        h = pad + self._key_h + pad
        self.resize(w, h)
        self._geo = None
        self.place(screen)
        self.show()

    def fade_for(self, rect) -> None:
        hg = self.frameGeometry()
        dx = max(hg.left() - rect.right(), rect.left() - hg.right(), 0)
        dy = max(hg.top() - rect.bottom(), rect.top() - hg.bottom(), 0)
        d = max(dx, dy)
        self.setWindowOpacity(min(1.0, 0.1 + d / 110.0 * 0.9))

    def place(self, screen) -> None:
        if screen is None:
            return None
        avail = screen.availableGeometry()
        if avail == self._geo:
            return None
        self._geo = avail
        self.move(avail.x() + (avail.width() - self.width()) // 2, avail.y() + avail.height() - self.height() - 36)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor('#3a3a38'), 1))
        p.setBrush(QColor(42, 42, 40, 235))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        pad = 20
        key = QRectF(pad, pad, self._key_w, self._key_h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawRoundedRect(key.adjusted(0, 3, 0, 3), 10, 10)
        p.setPen(QPen(QColor('#c9c9c7'), 1))
        p.setBrush(QColor('#f0f0f0'))
        p.drawRoundedRect(key, 10, 10)
        aw = self._arrow_w
        ax = key.x() + 18
        cy = key.center().y()
        base_y, tip_y = (cy - aw * 0.52, cy + aw * 0.52)
        wing_y = cy - aw * 0.02
        stem = aw * 0.42
        mid = ax + aw / 2
        path = QPainterPath()
        path.moveTo(mid, tip_y)
        path.lineTo(ax + aw, wing_y)
        path.lineTo(mid + stem / 2, wing_y)
        path.lineTo(mid + stem / 2, base_y)
        path.lineTo(mid - stem / 2, base_y)
        path.lineTo(mid - stem / 2, wing_y)
        path.lineTo(ax, wing_y)
        path.closeSubpath()
        p.setPen(QPen(QColor('#1f1f1e'), 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(QColor('#1f1f1e'))
        p.setFont(self._fkey)
        p.drawText(QRectF(ax + aw + 10, key.y(), key.right() - (ax + aw + 10), key.height()), Qt.AlignVCenter | Qt.AlignLeft, self._KEY)
        p.setPen(QColor('#c2c2c0'))
        p.setFont(self._ftxt)
        txt = QRectF(key.right() + 14, 0, self.width() - key.right() - 14 - pad, self.height())
        p.drawText(txt, Qt.AlignVCenter | Qt.AlignLeft, self._TEXT)


class _BrowseHint(QWidget):
    """Small pill shown below the cover-only overlay while hovering: tells you to just
    scroll (no button) to browse. The small cover window has no room for an inline hint,
    so this floats under it. Click-through, fades (opacity driven by the overlay)."""
    _TEXT = '↕  Scroll to browse'

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._f = QFont(_FONT, 10)
        self._f.setBold(True)
        fm = QFontMetrics(self._f)
        self.resize(fm.horizontalAdvance(self._TEXT) + 28, fm.height() + 14)
        self.show()

    def place(self, anchor) -> None:
        self.move(anchor.x() + (anchor.width() - self.width()) // 2, anchor.bottom() + 6)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor('#3a3a38'), 1))
        p.setBrush(QColor(42, 42, 40, 235))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
        p.setFont(self._f)
        p.setPen(QColor('#e8e8e6'))
        p.drawText(self.rect(), Qt.AlignCenter, self._TEXT)


class _ShortcutHud(QWidget):
    """Cheat-sheet shown while the side button is held + you hesitate: the music
    gestures, as light keycaps (matching the 'Shift to snap' hint). Click-through,
    bottom-center, fades in/out (opacity driven by the overlay)."""
    _ROWS = [('Scroll', 'Volume'), ('Left / Right', 'Prev / Next'), ('Space', 'Pause')]

    def __init__(self, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._fk = QFont(_FONT, 12)
        self._fk.setBold(True)
        self._fa = QFont(_FONT, 14)
        fmk, fma = (QFontMetrics(self._fk), QFontMetrics(self._fa))
        self._capw = [fmk.horizontalAdvance(k) + 26 for k, _ in self._ROWS]
        self._maxcap = max(self._capw)
        self._caph = fmk.height() + 12
        self._aw = max(fma.horizontalAdvance(a) for _, a in self._ROWS)
        self._pad = 18
        self._rgap = 10
        self._gap = 18
        w = self._pad * 2 + self._maxcap + self._gap + self._aw
        h = self._pad * 2 + self._caph * len(self._ROWS) + self._rgap * (len(self._ROWS) - 1)
        self.resize(w, h)
        self._geo = None
        self.place(screen)
        self.show()

    def place(self, screen) -> None:
        if screen is None:
            return None
        avail = screen.availableGeometry()
        if avail == self._geo:
            return None
        self._geo = avail
        self.move(avail.x() + (avail.width() - self.width()) // 2, avail.y() + avail.height() - self.height() - 36)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor('#3a3a38'), 1))
        p.setBrush(QColor(42, 42, 40, 235))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        ax = self._pad + self._maxcap + self._gap
        y = self._pad
        for i, (k, a) in enumerate(self._ROWS):
            cap = QRectF(self._pad, y, self._capw[i], self._caph)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRoundedRect(cap.adjusted(0, 2.5, 0, 2.5), 8, 8)
            p.setPen(QPen(QColor('#c9c9c7'), 1))
            p.setBrush(QColor('#f0f0f0'))
            p.drawRoundedRect(cap, 8, 8)
            p.setPen(QColor('#1f1f1e'))
            p.setFont(self._fk)
            p.drawText(cap, Qt.AlignCenter, k)
            p.setPen(QColor('#e8e8e6'))
            p.setFont(self._fa)
            p.drawText(QRectF(ax, y, self._aw, self._caph), Qt.AlignVCenter | Qt.AlignLeft, a)
            y += self._caph + self._rgap


class _VolumeCursorHud(QWidget):
    """Volume pill that follows the cursor while the side button is held + the
    volume just changed. Fades in on a vol change, fades out ~1s after the last
    change (even if side stays held). Click-through topmost, like _ShortcutHud."""

    def __init__(self, get_state):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._get = get_state
        self._W, self._H = (122, 30)
        self.resize(self._W, self._H)
        self._fn = QFont(_FONT, 11)
        self._fn.setBold(True)
        self._vol = 0.0
        self._vol_disp = 0.0
        self._last_vol = -1.0
        self._vol_t = 0.0
        self._alpha = 0.0
        self._last_tick_t = 0.0
        self._tm = QTimer(self)
        self._tm.timeout.connect(self._tick)
        self._tm.start(7)
        self.show()

    def _tick(self):
        st = self._get() or {}
        held = bool(st.get('mouse_held', False))
        vol = float(st.get('volume', 0.0) or 0.0)
        now = time.monotonic()
        if abs(vol - self._last_vol) > 0.0005:
            self._vol_t = now
            self._last_vol = vol
        self._vol = vol
        dt = now - self._last_tick_t if self._last_tick_t else 0.016
        self._last_tick_t = now
        import math
        bar_k = 1.0 - math.exp(-12.0 * dt)
        self._vol_disp += (self._vol - self._vol_disp) * bar_k
        target = 1.0 if held and now - self._vol_t < 1.0 else 0.0
        fade_rate = 20.0 if target > self._alpha else 8.0
        fade_k = 1.0 - math.exp(-fade_rate * dt)
        self._alpha += (target - self._alpha) * fade_k
        self._alpha = max(0.0, min(1.0, self._alpha))
        if self._alpha < 0.01:
            if self.isVisible():
                self.hide()
            return None
        if not self.isVisible():
            self.show()
        try:
            import ctypes
            from ctypes import wintypes

            class _P(ctypes.Structure):
                _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]
            p = _P()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
            cx, cy = (int(p.x), int(p.y))
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.screenAt(QPointF(cx, cy).toPoint()) or QGuiApplication.primaryScreen()
            geo = scr.availableGeometry()
            x = min(max(cx - self._W // 2, geo.left()), geo.right() - self._W)
            y = min(max(cy - self._H - 14, geo.top()), geo.bottom() - self._H)
            self.move(x, y)
        except Exception:
            pass
        self.update()

    def paintEvent(self, e):
        if self._alpha <= 0.01:
            return None
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self._alpha)
        p.setPen(QPen(QColor('#3a3a38'), 1))
        p.setBrush(QColor(42, 42, 40, 235))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        col = QColor('#e8e8e6')
        ix, iy = (12, self._H // 2)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRect(ix, iy - 3, 3, 6)
        from PySide6.QtGui import QPainterPath
        cone = QPainterPath()
        cone.moveTo(ix + 3, iy - 3)
        cone.lineTo(ix + 8, iy - 7)
        cone.lineTo(ix + 8, iy + 7)
        cone.lineTo(ix + 3, iy + 3)
        cone.closeSubpath()
        p.drawPath(cone)
        if self._vol <= 0.005:
            panel = QColor(42, 42, 40)
            p.setPen(QPen(panel, 5))
            p.drawLine(ix - 3, iy + 9, ix + 14, iy - 9)
            p.setPen(QPen(col, 2))
            p.drawLine(ix - 1, iy + 7, ix + 12, iy - 7)
        else:
            p.setPen(QPen(col, 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawArc(ix + 4, iy - 4, 7, 8, -832, 1664)
            if self._vol > 0.5:
                p.drawArc(ix + 3, iy - 7, 12, 14, -768, 1536)
        bx, by, bw, bh = (36, self._H // 2 - 3, 78, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 50))
        p.drawRoundedRect(bx, by, bw, bh, 3, 3)
        fw = int(bw * max(0.0, min(1.0, self._vol_disp)))
        if fw > 0:
            p.setBrush(col)
            p.drawRoundedRect(bx, by, fw, bh, 3, 3)


class SpotifyOverlay(QWidget):
    def __init__(self, get_state, media, on_move=None, on_move_mode=None, on_resize=None, on_snap=None, on_skip=None, on_hover=None):
        super().__init__()
        self._get_state = get_state
        self._media = media
        self._on_move = on_move
        self._on_move_mode = on_move_mode
        self._on_resize = on_resize
        self._on_snap = on_snap
        self._on_skip = on_skip
        self._on_hover = on_hover
        self._guides = None
        self._snap_axes = (None, None)
        self._move_mode = False
        self._dragging = False
        self._drag_off = None
        self._resizing = False
        self._rs_anchor = None
        self._rs_start_dist = 1.0
        self._rs_start_scale = 1.0
        self._pending_scale = None
        self._pending_scale_until = 0.0
        self.setMouseTracking(True)
        self._prev_lbtn = False
        self._last_click_t = 0.0
        self._move_toggle_cd = 0.0
        self._hovering = False
        self._hover_region = None
        self._pending_link = None
        self._link_click_t = 0.0
        self._hud = None
        self._hud_a = 0.0
        self._hint_a = 0.0
        self._bhint = None
        self._text_a = 1.0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(_W, _H)
        self._held = False
        self._queue = None
        self._strip_a = 0.0
        self._skip_until = 0.0
        self._strip_track = None
        self._held_since = 0.0
        self._held_prev = False
        self._slide = 0.0
        self._mskip_seq = None
        self._mbrowse_seq = None
        self._mskip_net = None
        self._nq = None
        self._nq_cur = None
        self._opt = 0
        self._goto_t = 0.0
        self._hold_uri = None
        self._hold_track = None
        self._ap_frozen = None
        self._ap_skip_t = 0.0
        self._ap_skip_from = None
        self._hold_t = 0.0
        self._hold_release_seq = 0
        self._disp_cur = None
        self._clu_change_t = 0.0
        self._last_skip_t = 0.0
        self._click_streak = 0
        self._click_t = 0.0
        self._big_pix = None
        self._big_cur = None
        self._paint_off = 0
        self._tile_pix = {}
        self._tile_img = {}
        self._tile_loading = set()
        self._last_position = ''
        self._reposition_from_state()
        self._alpha = 0.0
        self._vol = 0.0
        self._show_until = 0.0
        self._last_sig = None
        self._last_ping = 0.0
        self._last_topmost = 0.0
        self._prev_game_focused = False
        self._scale = 1.0
        self._compact = False
        self._cover_only = False
        self._cover_pix = None
        self._cover_track = None
        self._shadow_cache = {}
        self._title = self._artist = ''
        self._playing = self._muted = self._safe = False
        self._prev_muted = False
        self._prev_playing = False
        self._play_show_until = 0.0
        self._play_a = 0.0
        self._connect_skip_t = 0.0
        self._volume = 0.0
        self._prev_vol = 0.0
        self._vol_show_until = 0.0
        self._vol_visible = False
        self._slider_a = 0.0
        self.setWindowOpacity(0.0)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def showEvent(self, e):
        super().showEvent(e)
        self._set_click_through(not self._move_mode)
        self._ensure_topmost()

    def _set_click_through(self, on: bool) -> None:
        """Toggle Win32 click-through. On = clicks pass to the game (normal);
        off = the overlay grabs the mouse so it can be dragged (move mode).

        Also toggles WS_EX_NOACTIVATE: normally the overlay is non-activating
        (WA_ShowWithoutActivating) so it never steals focus from the game. But a
        non-activating topmost window won't receive drag input while ANOTHER Segue
        window (Controls/Preferences) is the active foreground window of the same
        process - so move mode looked "locked" until you closed that window. In
        move mode we make it activatable + activate it so the drag always lands;
        on exit we restore non-activating."""
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = (-20, 524288, 32, 134217728)
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= WS_EX_LAYERED
            if on:
                style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            else:
                style &= ~WS_EX_TRANSPARENT
                style &= ~WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            if not on:
                try:
                    self.raise_()
                    self.activateWindow()
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
        except Exception:
            pass

    def _ensure_topmost(self, strong: bool=False) -> None:
        """Re-assert HWND_TOPMOST. A WindowStaysOnTopHint overlay can lose its
        z-order when a game takes the foreground / switches to borderless
        fullscreen - it then sits BEHIND the game until re-shown (the user had
        to toggle the overlay off/on to fix it). Re-asserting periodically
        keeps it on top with no user action. SWP_NOSIZE|NOMOVE|NOACTIVATE so
        it neither moves nor steals focus.

        strong=True forces a re-stack at the TOP of the topmost band: a plain
        HWND_TOPMOST is a no-op when we're ALREADY topmost, so a game that went
        fullscreen after us stays on top. Dropping to NOTOPMOST then back to
        TOPMOST re-inserts us above it. Used on the game-foreground edge (the
        theft moment); the back-to-back calls make the gap imperceptible."""
        try:
            hwnd = int(self.winId())
            SWP = 19
            if strong:
                ctypes.windll.user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP)
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP)
        except Exception:
            pass

    def _reposition_from_state(self) -> None:
        """Read overlay_position (+ optional custom percent + scale) from the
        shared state dict + place + resize the window. Called once at init
        and again whenever any of those change (live reload from settings)."""
        try:
            st = self._get_state()
        except Exception:
            st = {}
        pos = st.get('overlay_position') or 'middle_left'
        _cxr = st.get('overlay_custom_x', -1.0)
        _cyr = st.get('overlay_custom_y', -1.0)
        cx = float(_cxr) if _cxr is not None else -1.0
        cy = float(_cyr) if _cyr is not None else -1.0
        scale = float(st.get('overlay_scale', 1.0) or 1.0)
        scale = max(0.5, min(2.0, scale))
        compact = bool(st.get('overlay_compact', False))
        mv = bool(st.get('overlay_move_mode', False))
        scr_name = st.get('overlay_screen') or ''
        cover_only = compact and self._text_a < 0.02
        sig = (pos, cx, cy, scale, compact, cover_only, scr_name, mv)
        if sig == self._last_position:
            return None
        self._last_position = sig
        self._scale = scale
        self._compact = compact
        self._cover_only = cover_only
        ch = int(_H * scale)
        cw = int((_COVER + _PAD * 2) * scale) if cover_only else int(_W * scale)
        reserve = not (cover_only or mv)
        up = _N_PREV * _TILE_PITCH if reserve else 0
        down = _N_NEXT * _TILE_PITCH if reserve else 0
        self._paint_off = up
        new_w = cw
        new_h = ch + int((up + down) * scale)
        self.resize(new_w, new_h)
        screen = None
        if scr_name:
            for s in QGuiApplication.screens():
                if s.name() == scr_name:
                    screen = s
                    break
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        geo = screen.geometry()
        anchor_w = int((_COVER + _PAD * 2) * scale) if compact else cw
        x, y = _overlay_xy(pos, geo.width(), geo.height(), anchor_w, ch, custom_x=cx, custom_y=cy)
        self.move(geo.x() + x, geo.y() + y - int(up * scale))

    def _tick(self):
        now = time.monotonic()
        st = self._get_state()
        move = bool(st.get('overlay_move_mode', False))
        if move != self._move_mode:
            self._move_mode = move
            self._set_click_through(not move)
            self.setCursor(Qt.SizeAllCursor if move else Qt.ArrowCursor)
            if move:
                try:
                    self._hint = _MoveHint(self.screen() or QGuiApplication.primaryScreen())
                except Exception:
                    self._hint = None
            elif getattr(self, '_hint', None) is not None:
                self._hint.close()
                self._hint = None
            self.update()
        self._update_hover(now)
        if self._hovering:
            self._show_until = now + 1.0
        acting = now < self._skip_until or now < getattr(self, '_vol_show_until', 0.0)
        hud_want = self._held and not self._move_mode and not self._compact and not acting and now - self._held_since > 0.5
        self._hud_a += ((1.0 if hud_want else 0.0) - self._hud_a) * 0.22
        if self._hud_a > 0.01:
            if self._hud is None:
                try:
                    self._hud = _ShortcutHud(self.screen() or QGuiApplication.primaryScreen())
                except Exception:
                    self._hud = None
            if self._hud is not None:
                self._hud.setWindowOpacity(self._hud_a)
        elif self._hud is not None:
            self._hud_a = 0.0
            try:
                self._hud.close()
            except Exception:
                pass
            self._hud = None
        hint_on = self._hovering and self._strip_a < 0.08 and not self._move_mode and bool(self._title) and self._nq is None
        self._hint_a += ((1.0 if hint_on else 0.0) - self._hint_a) * 0.2
        if self._hint_a < 0.01:
            self._hint_a = 0.0
        bh_on = self._compact and self._hint_a > 0.01
        if bh_on:
            if self._bhint is None:
                try:
                    self._bhint = _BrowseHint()
                except Exception:
                    self._bhint = None
            if self._bhint is not None:
                self._bhint.place(self.frameGeometry())
                self._bhint.setWindowOpacity(self._hint_a)
        elif self._bhint is not None:
            try:
                self._bhint.close()
            except Exception:
                pass
            self._bhint = None
        browsing = self._strip_a > 0.01 or now < self._skip_until
        t_target = 1.0 if not self._compact or browsing else 0.0
        t_ramp = 0.12 if self._compact else 0.32
        self._text_a += (t_target - self._text_a) * t_ramp
        if self._text_a < 0.01:
            self._text_a = 0.0
        elif self._text_a > 0.99:
            self._text_a = 1.0
        self._poll_double_click(now)
        if self._pending_scale is not None:
            if abs(float(st.get('overlay_scale', 1.0) or 1.0) - self._pending_scale) < 1e-06 or now >= self._pending_scale_until:
                self._pending_scale = None
        if (not self._move_mode or not (self._dragging or self._resizing)) and self._pending_scale is None:
            self._reposition_from_state()
        if move:
            self._show_until = now + 1.0
        ping = float(st.get('overlay_ping') or 0.0)
        if ping > self._last_ping:
            self._last_ping = ping
            self._show_until = now + _LINGER_S
        enabled = bool(st.get('overlay_enabled', True))
        self.setVisible(enabled)
        if enabled and now - self._last_topmost >= 1.0:
            self._last_topmost = now
            self._ensure_topmost()
        gf = enabled and bool(st.get('game_focused', False))
        if gf and not self._prev_game_focused:
            self._ensure_topmost(strong=True)
        self._prev_game_focused = gf
        self._volume = max(0.0, min(1.0, float(st.get('volume', 0.0))))
        self._muted = bool(st.get('muted', False))
        self._safe = bool(st.get('safe', False))
        np = self._media.get()
        self._title = np.title if np else ''
        self._artist = np.artist if np else ''
        self._playing = np.is_playing if np else False
        self._connect_skip_t = float(st.get('_connect_skip_t', 0.0) or 0.0)
        if not self._playing and now - self._connect_skip_t < 1.0:
            self._playing = True
        if self._prev_muted and not self._muted:
            self._alpha = 0.0
        self._prev_muted = self._muted
        if self._playing and not self._prev_playing:
            self._play_show_until = now + 1.5
        self._prev_playing = self._playing
        play_target = 1.0 if self._playing and now < self._play_show_until else 0.0
        self._play_a += (play_target - self._play_a) * 0.16
        sig = (self._title, self._artist, round(self._volume, 2), self._muted, self._safe, self._playing)
        if sig != self._last_sig:
            self._show_until = now + _LINGER_S
            self._last_sig = sig
        if abs(self._volume - self._prev_vol) > 0.003:
            self._vol_show_until = now + 1.6
        self._prev_vol = self._volume
        self._vol_visible = now < self._vol_show_until
        self._slider_a += ((1.0 if self._vol_visible else 0.0) - self._slider_a) * 0.16
        always = bool(st.get('overlay_always_on', False))
        _hide_menu = st.get('overlay_in_game_only', False) and not st.get('game_focused', False)
        _hide_drive = st.get('overlay_drive_only', False) and not st.get('can_skip', True)
        if (_hide_menu or _hide_drive) and not self._move_mode:
            target = 0.0
        else:
            target = 1.0 if always or self._move_mode or now < self._show_until else 0.0
        ease = 0.16 if target > self._alpha else 0.1
        self._alpha += (target - self._alpha) * ease
        self._alpha = max(0.0, min(0.98, self._alpha))
        self.setWindowOpacity(self._alpha)
        self._vol += (self._volume - self._vol) * 0.5
        _thumb = np.thumb if np else None
        cover_changed = False
        if _thumb != getattr(self, '_cover_src', False):
            self._cover_src = _thumb
            cover_changed = True
            if _thumb:
                img = QImage.fromData(_thumb)
                self._cover_pix = QPixmap.fromImage(img).scaled(_COVER, _COVER, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation) if not img.isNull() else None
            else:
                self._cover_pix = None
        self._held = bool(st.get('mouse_held', False))
        rel_seq = int(st.get('_ovl_hold_release', 0) or 0)
        if rel_seq != self._hold_release_seq:
            self._hold_release_seq = rel_seq
            self._hold_uri = None
            self._opt = 0
        nq = st.get('np_queue')
        nq = nq if isinstance(nq, dict) else None
        ap = bool(nq and nq.get('autoplay'))
        if nq:
            rc = (nq.get('current') or {}).get('uri')
            if ap:
                if self._opt != 0 and self._ap_frozen is None:
                    self._ap_frozen = nq
                    self._ap_skip_from = None
                    _clog("AP-FREEZE opt={} cur='{}' n0='{}'".format(self._opt, ((nq.get('current') or {}).get('title') or '')[:14], ((nq.get('next') or [{}])[0].get('title') or '')[:14]))
                elif self._ap_frozen is not None and self._ap_skip_from is not None and now - self._last_skip_t > 0.6:
                    if rc != self._ap_skip_from and now - self._clu_change_t > 1.0 or now - self._ap_skip_t > 6.0:
                        self._ap_frozen = None
                        self._hold_uri = None
                        self._opt = 0
                        _clog("AP-RESYNC -> live cur='{}'".format(((nq.get('current') or {}).get('title') or '')[:14]))
            else:
                self._ap_frozen = None
            if self._nq is None:
                self._opt = 0
            elif ap:
                if rc != self._nq_cur:
                    self._clu_change_t = now
            elif self._hold_uri is not None:
                flat = (nq.get('prev') or []) + ([nq['current']] if nq.get('current') else []) + (nq.get('next') or [])
                base = len(nq.get('prev') or [])
                if rc == self._hold_uri:
                    self._opt = 0
                    self._hold_uri = None
                else:
                    idx = next((i for i, t in enumerate(flat) if t.get('uri') == self._hold_uri), None)
                    if idx is not None:
                        self._opt = idx - base
                    elif now - self._hold_t > 3.0:
                        self._hold_uri = None
                if rc != self._nq_cur:
                    self._clu_change_t = now
                    _clog("HOLD opt={} landed={} clu='{}'".format(self._opt, int(self._hold_uri is None), ((nq.get('current') or {}).get('title') or '')[:18]))
            elif rc != self._nq_cur:
                self._clu_change_t = now
                if now - self._last_skip_t < 2.0:
                    old = (self._nq.get('prev') or []) + ([self._nq['current']] if self._nq.get('current') else []) + (self._nq.get('next') or [])
                    ob = len(self._nq.get('prev') or [])
                    ni = next((i for i, t in enumerate(old) if t.get('uri') == rc), None)
                    if ni is not None:
                        prev_opt = self._opt
                        self._opt -= ni - ob
                        if prev_opt > 0:
                            self._opt = max(0, self._opt)
                        elif prev_opt < 0:
                            self._opt = min(0, self._opt)
                else:
                    self._opt = 0
                _clog("CLU brow={} -> opt={} clu='{}'".format(int(now < self._skip_until), self._opt, ((nq.get('current') or {}).get('title') or '')[:18]))
            self._nq, self._nq_cur = (nq, rc)
        elif not self._held and self._strip_a < 0.02:
            self._nq = self._nq_cur = None
            self._opt = 0
        if self._goto_t and now >= self._goto_t and self._opt != 0 and self._nq is not None and self._on_skip:
            self._goto_t = 0.0
            bq = self._ap_frozen if self._nq.get('autoplay') and self._ap_frozen is not None else self._nq
            flat = (bq.get('prev') or []) + ([bq['current']] if bq.get('current') else []) + (bq.get('next') or [])
            pos = len(bq.get('prev') or []) + self._opt
            if 0 <= pos < len(flat):
                target = (flat[pos] or {}).get('uri')
                if target:
                    self._hold_uri = target
                    self._hold_track = flat[pos]
                    self._hold_t = now
                    if self._nq.get('autoplay'):
                        _clog("AP-SETTLE opt={} pos={} frozen={} tgt='{}'".format(self._opt, pos, self._ap_frozen is not None, (flat[pos].get('title') or '')[:16]))
                    try:
                        self._on_skip(self._opt)
                    except Exception:
                        pass
                    if self._nq.get('autoplay'):
                        self._ap_skip_t = now
                        self._ap_skip_from = self._nq_cur
                    if self._nq.get('autoplay') and self._ap_frozen is not None:
                        self._ap_frozen = {'autoplay': True, 'current': flat[pos], 'prev': flat[:pos], 'next': flat[pos + 1:]}
                        self._opt = 0
        self._queue = self._derive()
        self._disp_cur = (self._queue.get('current') or {}).get('uri') if self._queue else None
        if not self._held and self._opt != 0 and self._nq is not None and self._hold_uri is None and now - self._clu_change_t > 0.4 and now - self._last_skip_t > 2.0:
            self._slide = max(-2.0, min(2.0, float(-self._opt))) * _TILE_PITCH
            self._opt = 0
            self._queue = self._derive()
            self._disp_cur = (self._queue.get('current') or {}).get('uri') if self._queue else None
            _clog("SETTLE-FIX -> reality clu='{}'".format((((self._queue or {}).get('current') or {}).get('title') or '')[:18]))
        nq = self._ap_frozen if self._ap_frozen is not None and self._nq and self._nq.get('autoplay') else self._nq
        if nq:
            tr = (nq.get('prev') or []) + ([nq['current']] if nq.get('current') else []) + (nq.get('next') or [])
            if tr:
                base = len(nq.get('prev') or [])
                pos = max(0, min(len(tr) - 1, base + self._opt))
                self._cover(tr[pos].get('art'), _COVER)
                if self._hold_uri is not None and self._hold_track is not None:
                    self._cover(self._hold_track.get('art'), _COVER)
                if pos + 1 < len(tr):
                    self._cover(tr[pos + 1].get('art'), _COVER)
                if pos - 1 >= 0:
                    self._cover(tr[pos - 1].get('art'), _COVER)
                lo = max(0, pos - _N_PREV - 5)
                hi = min(len(tr), pos + _N_NEXT + 1 + 5)
                for t in tr[lo:hi]:
                    self._cover(t.get('art'), _TILE)
        tk = (self._title, self._artist)
        if self._held and not self._held_prev:
            self._held_since = now
        self._held_prev = self._held
        if self._held:
            if now - self._held_since > 0.25:
                self._show_until = now + 1.0
            self._strip_track = tk
        else:
            self._strip_track = tk
        strip_on = 1.0 if (self._held or self._hovering and now < self._skip_until) and self._queue and not self._cover_only else 0.0
        if strip_on or self._strip_a > 0.01:
            self._show_until = now + 1.0
        self._strip_a += (strip_on - self._strip_a) * (0.12 if strip_on > self._strip_a else 0.1)
        self._strip_a = max(0.0, min(1.0, self._strip_a))
        seq = int(st.get('mskip_seq', 0) or 0)
        bseq = int(st.get('mbrowse_seq', 0) or 0)
        net = int(st.get('mskip_net', 0) or 0)
        if self._mskip_seq is None:
            self._mskip_seq, self._mbrowse_seq, self._mskip_net = (seq, bseq, net)
        elif seq != self._mskip_seq:
            n = net - self._mskip_net
            wake = bseq != self._mbrowse_seq
            visible = now < self._skip_until or self._strip_a > 0.05
            if not wake:
                self._click_streak = self._click_streak + 1 if now - self._click_t < 1.2 else 1
                self._click_t = now
                if self._click_streak >= 2:
                    wake = True
            else:
                self._click_streak = 0
            self._mskip_seq, self._mbrowse_seq, self._mskip_net = (seq, bseq, net)
            _clog('MSKIP seq={} n={} wake={} vis={} nq={} held={}'.format(seq, n, int(wake), int(visible), self._nq is None, self._held))
            if n != 0 and self._nq and (-64 < n < 64):
                self._opt += n
                self._last_skip_t = now
                self._hold_uri = None
                self._goto_t = now + _GOTO_SETTLE
                if wake or visible:
                    self._slide = (1 if n > 0 else -1) * _TILE_PITCH
                    self._skip_until = now + _SKIP_WIN
                self._queue = self._derive()
                self._disp_cur = (self._queue.get('current') or {}).get('uri') if self._queue else None
                _clog("SKIP n={} wake={} vis={} -> opt={} disp='{}' np='{}'".format(n, int(wake), int(visible), self._opt, (((self._queue or {}).get('current') or {}).get('title') or '')[:18], (self._title or '')[:18]))
        self._slide += (0.0 - self._slide) * 0.18
        if abs(self._slide) < 0.5:
            self._slide = 0.0
        self._timer.setInterval(16 if self._strip_a > 0.01 or abs(self._slide) > 0.5 or 0.01 < self._hud_a < 0.99 or 0.01 < self._hint_a < 0.99 or 0.01 < self._text_a < 0.99 else 33)
        qsig = None
        if self._queue:
            qsig = ((self._queue.get('current') or {}).get('uri'), tuple((t.get('uri') for t in self._queue.get('next') or [])), tuple((t.get('uri') for t in self._queue.get('prev') or [])))
        dirty = cover_changed or sig != getattr(self, '_paint_sig', None) or abs(self._alpha - getattr(self, '_paint_alpha', -1)) > 0.005 or abs(self._play_a - getattr(self, '_paint_play_a', -1)) > 0.005 or abs(self._slider_a - getattr(self, '_paint_slider_a', -1)) > 0.005 or abs(self._vol - getattr(self, '_paint_vol', -1)) > 0.003 or abs(self._strip_a - getattr(self, '_paint_strip_a', -1)) > 0.005 or abs(self._hint_a - getattr(self, '_paint_hint_a', -1)) > 0.01 or abs(self._text_a - getattr(self, '_paint_text_a', -1)) > 0.01 or abs(self._slide) > 0.5 or len(self._tile_img) != getattr(self, '_paint_imgs', -1) or qsig != getattr(self, '_paint_qsig', None)
        if dirty:
            self._paint_sig = sig
            self._paint_alpha = self._alpha
            self._paint_play_a = self._play_a
            self._paint_slider_a = self._slider_a
            self._paint_vol = self._vol
            self._paint_strip_a = self._strip_a
            self._paint_hint_a = self._hint_a
            self._paint_text_a = self._text_a
            self._paint_qsig = qsig
            self._paint_imgs = len(self._tile_img)
            self.update()

    def _label_shadow(self, text, size, bold):
        """Cache a Gaussian-blurred black render of `text` for a soft drop shadow."""
        key = (text, size, bold)
        hit = self._shadow_cache.get(key)
        if hit:
            return hit
        f = QFont(_FONT, size)
        f.setBold(bold)
        fm = QFontMetrics(f)
        asc = fm.ascent()
        w = max(1, fm.horizontalAdvance(text))
        h = max(1, fm.height())
        m = 8
        img = QImage(w + 2 * m, h + 2 * m, QImage.Format_ARGB32)
        img.fill(0)
        pp = QPainter(img)
        pp.setFont(f)
        pp.setPen(QColor(0, 0, 0))
        pp.drawText(m, m + asc, text)
        pp.end()
        raw = img.bits().tobytes()
        pil = Image.frombuffer('RGBA', (img.width(), img.height()), raw, 'raw', 'BGRA', 0, 1)
        pil = pil.filter(ImageFilter.GaussianBlur(3))
        out = QImage(pil.tobytes('raw', 'BGRA'), pil.width, pil.height, QImage.Format_ARGB32).copy()
        pix = QPixmap.fromImage(out)
        if len(self._shadow_cache) > 40:
            self._shadow_cache.clear()
        res = (pix, m, asc)
        self._shadow_cache[key] = res
        return res

    def _text(self, p, x, y, text, color, size, bold=False, right=False, maxw=None):
        f = QFont(_FONT, size)
        f.setBold(bold)
        fm = QFontMetrics(f)
        if maxw:
            text = fm.elidedText(text, Qt.ElideRight, maxw)
        if right:
            x -= fm.horizontalAdvance(text)
        pix, m, asc = self._label_shadow(text, size, bold)
        prev = p.opacity()
        p.setOpacity(prev * 0.6)
        p.drawPixmap(int(x - m), int(y - asc - m + 2), pix)
        p.setOpacity(prev)
        p.setFont(f)
        p.setPen(color)
        p.drawText(int(x), int(y), text)
        return fm.horizontalAdvance(text)

    def _badge(self, p, cx, cy, r):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 8, 12, 235))
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

    def _soft_shadow(self, p, x, y, w, h, r, layers=9):
        p.setPen(Qt.NoPen)
        for i in range(layers, 0, -1):
            p.setBrush(QColor(0, 0, 0, 5))
            p.drawRoundedRect(QRectF(x - i + 1, y - i + 3, w + 2 * i, h + 2 * i), r + i, r + i)

    def _mute_glyph(self, p, cx, cy):
        p.setPen(Qt.NoPen)
        p.setBrush(_WHITE)
        p.drawRect(int(cx - 8), int(cy - 4), 4, 8)
        cone = QPainterPath()
        cone.moveTo(cx - 4, cy - 4)
        cone.lineTo(cx + 1, cy - 9)
        cone.lineTo(cx + 1, cy + 9)
        cone.lineTo(cx - 4, cy + 4)
        cone.closeSubpath()
        p.drawPath(cone)
        p.setPen(QPen(QColor(8, 8, 12), 5))
        p.drawLine(int(cx - 8), int(cy + 7), int(cx + 7), int(cy - 8))
        p.setPen(QPen(_WHITE, 2.4))
        p.drawLine(int(cx - 8), int(cy + 7), int(cx + 7), int(cy - 8))

    def _play_glyph(self, p, cx, cy):
        p.setPen(Qt.NoPen)
        p.setBrush(_WHITE)
        tri = QPainterPath()
        tri.moveTo(cx - 5, cy - 8)
        tri.lineTo(cx - 5, cy + 8)
        tri.lineTo(cx + 9, cy)
        tri.closeSubpath()
        p.drawPath(tri)

    def _pause_glyph(self, p, cx, cy):
        p.setPen(Qt.NoPen)
        p.setBrush(_WHITE)
        p.drawRoundedRect(QRectF(cx - 7, cy - 8, 4.5, 16), 1.5, 1.5)
        p.drawRoundedRect(QRectF(cx + 2.5, cy - 8, 4.5, 16), 1.5, 1.5)

    def _speaker(self, p, x, y, vol):
        col = _WHITE
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRect(int(x), int(y - 3), 3, 6)
        cone = QPainterPath()
        cone.moveTo(x + 3, y - 3)
        cone.lineTo(x + 8, y - 7)
        cone.lineTo(x + 8, y + 7)
        cone.lineTo(x + 3, y + 3)
        cone.closeSubpath()
        p.drawPath(cone)
        if vol <= 0.005:
            p.setPen(QPen(col, 2))
            p.drawLine(int(x + 11), int(y - 5), int(x + 18), int(y + 5))
        else:
            p.setPen(QPen(col, 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawArc(int(x + 4), int(y - 4), 7, 8, -832, 1664)
            if vol > 0.5:
                p.drawArc(int(x + 3), int(y - 7), 12, 14, -768, 1536)

    def _lock_icon(self, p, cx, cy):
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_WHITE, 2))
        p.drawArc(int(cx - 5), int(cy - 9), 10, 10, 0, 2880)
        p.setPen(Qt.NoPen)
        p.setBrush(_WHITE)
        p.drawRoundedRect(QRectF(cx - 6, cy - 1, 12, 9), 2, 2)

    _GRIP = 16

    def _corner_at(self, pos):
        """Which resize corner `pos` (widget-local) falls in, or None."""
        g = max(12, int(self._GRIP * self._scale))
        w, h = (self.width(), self.height())
        left, top = (pos.x() <= g, pos.y() <= g)
        right, bottom = (pos.x() >= w - g, pos.y() >= h - g)
        if top and left:
            return 'tl'
        if top and right:
            return 'tr'
        if bottom and left:
            return 'bl'
        if bottom and right:
            return 'br'
        return None

    def _region_at(self, pos):
        """Which now-playing TEXT element the widget point falls on ('title'/'artist'),
        or None. The cover is deliberately NOT a link - it's the double-click-to-move
        target, and the title already opens the album. Maps widget px -> paint space."""
        if self._compact or self._move_mode or not self._title:
            return None
        s = self._scale or 1.0
        x = pos.x() / s
        y = pos.y() / s - self._paint_off
        tx = _PAD + _COVER + 16
        if tx <= x <= _W - _PAD:
            if 38 <= y <= 62:
                return 'title'
            if 64 <= y <= 90 and self._artist:
                return 'artist'
        return None

    def _fire_link(self):
        kind, self._pending_link = (self._pending_link, None)
        if kind and not self._move_mode:
            self._open_link(kind)

    def _open_link(self, kind):
        """Open the playing track's exact Spotify page (kind 'album'/'artist').
        Resolve name -> id via the getsegue.app worker, fall back to a search.
        Network + shell open run off the GUI thread."""
        track, artist = (self._title, self._artist)
        if kind == 'artist' and not artist or not (track or artist):
            return None

        def _run():
            try:
                from fh6_spotify import spotify_links as _sl
                r = _sl.resolve(track, artist)
                uri = (r.get(kind) or {}).get('uri') if r else ''
                _clog('LINK kind={} track={!r} artist={!r} resolved={} uri={!r}'.format(kind, track, artist, bool(r), uri))
                if not uri:
                    uri = _sl.search_uri(artist if kind == 'artist' else '{} {}'.format(track, artist))
                if uri:
                    os.startfile(uri)
            except Exception as e:
                _clog('LINK EXC {}'.format(e))
        threading.Thread(target=_run, daemon=True).start()

    def _apply_live_scale(self, scale, anchor, corner):
        """Resize around the FIXED opposite corner so the grabbed corner tracks
        the cursor. `anchor` = global QPoint of the fixed corner."""
        self._scale = scale
        base_w = _COVER + _PAD * 2 if self._compact else _W
        base_h = _COVER + _PAD * 2 if self._compact else _H
        nw, nh = (int(base_w * scale), int(base_h * scale))
        x = anchor.x() - nw if corner in ('tl', 'bl') else anchor.x()
        y = anchor.y() - nh if corner in ('tl', 'tr') else anchor.y()
        self.setGeometry(x, y, nw, nh)
        self.update()

    def mousePressEvent(self, e):
        dbl = False
        if e.button() == Qt.LeftButton:
            t = time.monotonic()
            dbl = t - self._link_click_t < 0.5
            self._link_click_t = t
            if dbl:
                self._pending_link = None
        if self._move_mode and e.button() == Qt.LeftButton:
            corner = self._corner_at(e.position().toPoint())
            if corner:
                fg = self.frameGeometry()
                self._rs_anchor = {'tl': fg.bottomRight(), 'tr': fg.bottomLeft(), 'bl': fg.topRight(), 'br': fg.topLeft()}[corner]
                self._rs_corner = corner
                gp = e.globalPosition().toPoint()
                self._rs_start_dist = max(1.0, ((gp.x() - self._rs_anchor.x()) ** 2 + (gp.y() - self._rs_anchor.y()) ** 2) ** 0.5)
                self._rs_start_scale = self._scale
                self._resizing = True
            else:
                self._dragging = True
                self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return None
        if e.button() == Qt.LeftButton and not dbl:
            reg = self._region_at(e.position().toPoint())
            if reg:
                self._pending_link = 'artist' if reg == 'artist' else 'album'
                QTimer.singleShot(520, self._fire_link)
                e.accept()

    def mouseMoveEvent(self, e):
        if self._move_mode and self._resizing:
            gp = e.globalPosition().toPoint()
            dist = max(1.0, ((gp.x() - self._rs_anchor.x()) ** 2 + (gp.y() - self._rs_anchor.y()) ** 2) ** 0.5)
            scale = max(0.6, min(2.0, self._rs_start_scale * dist / self._rs_start_dist))
            self._apply_live_scale(scale, self._rs_anchor, self._rs_corner)
            e.accept()
            return None
        if self._move_mode and self._dragging and self._drag_off is not None:
            gp = e.globalPosition().toPoint()
            target = gp - self._drag_off
            if getattr(self, '_hint', None) is not None:
                self._hint.place(QGuiApplication.screenAt(gp))
                self._hint.fade_for(self.frameGeometry().translated(target - self.frameGeometry().topLeft()))
            if e.modifiers() & Qt.ShiftModifier:
                scr = QGuiApplication.screenAt(gp) or QGuiApplication.primaryScreen()
                if self._guides is None or self._guides.geometry() != scr.geometry():
                    if self._guides is not None:
                        self._guides.close()
                    self._guides = _SnapGuides(scr)
                    self.raise_()
                geo = scr.geometry()
                w, h = (self.width(), self.height())
                tx = target.x() - geo.x()
                ty = target.y() - geo.y()
                m = _SNAP_MARGIN
                xc = [(m, 'left', m), ((geo.width() - w) // 2, 'center', geo.width() / 2), (geo.width() - w - m, 'right', geo.width() - m)]
                yc = [(m, 'top', m), ((geo.height() - h) // 2, 'middle', geo.height() / 2), (geo.height() - h - m, 'bottom', geo.height() - m)]
                xname = yname = None
                vline = hline = None
                for sx, name, gl in xc:
                    if abs(tx - sx) <= _SNAP_THRESH:
                        tx, xname, vline = (sx, name, gl)
                        break
                for sy, name, gl in yc:
                    if abs(ty - sy) <= _SNAP_THRESH:
                        ty, yname, hline = (sy, name, gl)
                        break
                self._guides.set_lines(vline, hline)
                self._snap_axes = (xname, yname)
                self.move(geo.x() + tx, geo.y() + ty)
            else:
                if self._guides is not None:
                    self._guides.close()
                    self._guides = None
                self._snap_axes = (None, None)
                self.move(target)
            e.accept()
            return None
        if self._move_mode:
            corner = self._corner_at(e.position().toPoint())
            cur = {None: Qt.SizeAllCursor, 'tl': Qt.SizeFDiagCursor, 'br': Qt.SizeFDiagCursor, 'tr': Qt.SizeBDiagCursor, 'bl': Qt.SizeBDiagCursor}[corner]
            self.setCursor(cur)
            return None
        reg = self._region_at(e.position().toPoint())
        if reg != self._hover_region:
            self._hover_region = reg
            self.update()

    def mouseReleaseEvent(self, e):
        if self._move_mode and self._resizing and e.button() == Qt.LeftButton:
            self._resizing = False
            self._persist_scale()
            self._persist_position()
            e.accept()
            return None
        if self._move_mode and self._dragging and e.button() == Qt.LeftButton:
            self._dragging = False
            xname, yname = self._snap_axes
            if self._guides is not None:
                self._guides.close()
                self._guides = None
            self._snap_axes = (None, None)
            if xname and yname and self._on_snap:
                scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
                primary = QGuiApplication.primaryScreen()
                sname = '' if primary is not None and scr is primary else scr.name()
                try:
                    self._on_snap(f'{yname}_{xname}', sname)
                except Exception:
                    pass
            else:
                self._persist_position()
            e.accept()

    def wheelEvent(self, e):
        if self._move_mode:
            step = 0.05 if e.angleDelta().y() > 0 else -0.05
            scale = max(0.6, min(2.0, self._scale + step))
            if abs(scale - self._scale) < 1e-09:
                return None
            center = self.frameGeometry().topLeft()
            self._apply_live_scale(scale, center, 'br')
            self._persist_scale()
            self._persist_position()
            e.accept()
            return None
        if not self._hovering or self._held or not self._nq:
            return None
        dy = e.angleDelta().y()
        if not dy:
            return None
        d = -1 if dy > 0 else 1
        if d < 0 and not (self._nq.get('prev') or []):
            return None
        now = time.monotonic()
        self._opt += d
        self._slide = d * _TILE_PITCH
        self._skip_until = now + _SKIP_WIN
        self._last_skip_t = now
        self._queue = self._derive()
        self._disp_cur = (self._queue.get('current') or {}).get('uri') if self._queue else None
        self._hold_uri = None
        self._goto_t = now + _GOTO_SETTLE
        self.update()
        e.accept()

    def _persist_scale(self) -> None:
        """Push the live scale to config via on_resize, and hold repositioning
        until the shared state reflects it (one runner tick of lag)."""
        self._pending_scale = self._scale
        self._pending_scale_until = time.monotonic() + 1.5
        if self._on_resize:
            try:
                self._on_resize(round(self._scale, 3))
            except Exception:
                pass

    def mouseDoubleClickEvent(self, e):
        if self._on_move_mode and e.button() == Qt.LeftButton:
            self._dragging = False
            self._move_toggle_cd = time.monotonic() + 0.45
            try:
                self._on_move_mode(not self._move_mode)
            except Exception:
                pass
            e.accept()

    def _persist_position(self) -> None:
        """Drop -> store the window's top-left as 0..1 percentages of WHICHEVER
        screen it was dropped on, plus that screen's name, via the on_move
        callback (config.overlay_custom_x/y + overlay_screen). Dragging onto a
        second monitor just works - the overlay lives there from then on."""
        try:
            scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
            geo = scr.geometry()
            sx = max(1, geo.width() - self.width())
            sy = max(1, geo.height() - self.height())
            cx = max(0.0, min(1.0, (self.x() - geo.x()) / sx))
            cy = max(0.0, min(1.0, (self.y() - geo.y()) / sy))
            primary = QGuiApplication.primaryScreen()
            sname = '' if primary is not None and scr is primary else scr.name()
            if self._on_move:
                self._on_move(cx, cy, sname)
        except Exception:
            pass

    def _update_hover(self, now: float) -> None:
        """Show a hand cursor (and become click-receptive) while the mouse is over
        the visible overlay, so it reads as interactive. In normal mode the window
        is click-through and gets no cursor of its own, so we detect the hover via
        the physical cursor position and toggle click-through off only while hovered
        (restored the moment the cursor leaves)."""
        if self._move_mode:
            return None
        try:
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            fg = self.frameGeometry()
            if self._strip_a > 0.01:
                over = self._alpha >= 0.5 and fg.contains(pt.x, pt.y)
            else:
                up_px = int(self._paint_off * self._scale)
                dn_px = 0 if self._cover_only else int(_N_NEXT * _TILE_PITCH * self._scale)
                over = self._alpha >= 0.5 and fg.left() <= pt.x <= fg.right() and fg.top() + up_px <= pt.y <= fg.bottom() - dn_px
            if over != self._hovering:
                self._hovering = over
                self._set_click_through(not over)
                self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
                if self._on_hover:
                    try:
                        self._on_hover(over)
                    except Exception:
                        pass
                if not over and self._hover_region is not None:
                    self._hover_region = None
                    self.update()
        except Exception:
            pass

    def _poll_double_click(self, now: float) -> None:
        """Edge-detect a physical left double-click inside the overlay rect and
        toggle move mode. Works even when the window is click-through (Qt gets no
        events). Guards: ignore while dragging, during a short post-toggle
        cooldown, and when the overlay is faded out (so it can't hijack in-game
        double-clicks that happen to land where the overlay would be)."""
        if self._on_move_mode is None or self._dragging:
            return None
        try:
            down = bool(ctypes.windll.user32.GetAsyncKeyState(1) & 32768)
        except Exception:
            return None
        edge = down and not self._prev_lbtn
        self._prev_lbtn = down
        if now < self._move_toggle_cd or self._alpha < 0.5 or not edge:
            return None
        try:
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        except Exception:
            return None
        if not self.frameGeometry().contains(pt.x, pt.y):
            self._last_click_t = 0.0
            if self._move_mode:
                self._move_toggle_cd = now + 0.45
                try:
                    self._on_move_mode(False)
                except Exception:
                    pass
            return None
        if now - self._last_click_t <= 0.4:
            self._last_click_t = 0.0
            self._move_toggle_cd = now + 0.45
            try:
                self._on_move_mode(not self._move_mode)
            except Exception:
                pass
        else:
            self._last_click_t = now

    def _cover(self, url, size):
        """Cached cover at `size` (QPixmap built on the GUI thread; the QImage is
        fetched off-thread and kept so any size can be derived). None until it lands."""
        if not url:
            return None
        key = (url, size)
        p = self._tile_pix.get(key)
        if p is not None:
            return p
        img = self._tile_img.get(url)
        if img is not None and not img.isNull():
            p = QPixmap.fromImage(img).scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self._tile_pix[key] = p
            if len(self._tile_pix) > 256:
                for k in list(self._tile_pix)[:96]:
                    self._tile_pix.pop(k, None)
            return p
        if url not in self._tile_loading:
            self._tile_loading.add(url)
            threading.Thread(target=self._fetch_tile, args=(url,), daemon=True).start()

    def _fetch_tile(self, url):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Segue'})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read()
            img = QImage.fromData(data)
            if not img.isNull():
                self._tile_img[url] = img
                if len(self._tile_img) > 256:
                    for k in list(self._tile_img)[:96]:
                        self._tile_img.pop(k, None)
        except Exception:
            pass
        finally:
            self._tile_loading.discard(url)

    def _derive(self):
        """Display queue = cluster shifted by our optimistic offset. Pure (no mutation);
        offset clamped to real depth so rapid skipping can't desync or flash."""
        nq = self._nq
        if nq and nq.get('autoplay') and self._ap_frozen is not None:
            nq = self._ap_frozen
        if not nq:
            return None
        tr = (nq.get('prev') or []) + ([nq['current']] if nq.get('current') else []) + (nq.get('next') or [])
        if not tr:
            return None
        base = len(nq.get('prev') or [])
        pos = max(0, min(len(tr) - 1, base + self._opt))
        self._opt = pos - base
        cur = tr[pos]
        if self._hold_uri is not None and self._hold_track is not None:
            cur = self._hold_track
        return {'current': cur, 'next': tr[pos + 1:pos + 1 + _N_NEXT], 'prev': tr[max(0, pos - _N_PREV):pos]}

    def _draw_tile(self, p, cx, top, pix, op):
        """One strip cover, centred at column x=cx, top edge `top`, opacity op."""
        if pix is None:
            return None
        x = cx - _TILE / 2
        p.setOpacity(op)
        self._soft_shadow(p, x, top, _TILE, _TILE, _TILE_CR, layers=4)
        path = QPainterPath()
        path.addRoundedRect(x, top, _TILE, _TILE, _TILE_CR, _TILE_CR)
        p.setClipPath(path)
        p.drawPixmap(int(x), int(top), pix)
        p.setClipping(False)
        p.setOpacity(1.0)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._move_mode:
            p.save()
            p.setPen(QPen(QColor('#FF7A1A'), 2, Qt.DashLine))
            p.setBrush(QColor(255, 122, 26, 28))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 10, 10)
            p.restore()
        if self._scale != 1.0:
            p.scale(self._scale, self._scale)
        if self._paint_off:
            p.translate(0, self._paint_off)
        cy = (_H - _COVER) // 2 + self._slide
        ccx, ccy = (_PAD + _COVER / 2, cy + _COVER / 2)
        card_cur = None
        card_pix = self._cover_pix
        if self._strip_a > 0.01 and self._queue:
            cc = self._queue.get('current') or {}
            cp = self._cover(cc.get('art'), _COVER)
            if cp is not None:
                self._big_pix, self._big_cur = (cp, cc)
            if self._big_cur is not None and self._big_pix is not None:
                card_cur, card_pix = (self._big_cur, self._big_pix)
        elif self._big_cur is not None and self._big_pix is not None and (self._nq and (self._nq.get('current') or {}).get('uri') == self._big_cur.get('uri') or (self._big_cur.get('title') or '') == (self._title or '') or (self._nq and self._nq.get('autoplay') and self._ap_frozen is not None)):
            card_cur, card_pix = (self._big_cur, self._big_pix)
        else:
            ccur = (self._nq.get('current') if self._nq else None) or {}
            cpix = self._cover(ccur.get('art'), _COVER) if ccur.get('art') else None
            if cpix is not None:
                self._big_pix, self._big_cur = (cpix, ccur)
                card_cur, card_pix = (ccur, cpix)
            elif ccur.get('art') and self._big_pix is not None:
                card_cur, card_pix = (self._big_cur, self._big_pix)
            else:
                self._big_cur = None
        self._soft_shadow(p, _PAD, cy, _COVER, _COVER, _CR)
        if card_pix is not None:
            path = QPainterPath()
            path.addRoundedRect(_PAD, cy, _COVER, _COVER, _CR, _CR)
            p.setClipPath(path)
            p.drawPixmap(_PAD, int(cy), card_pix)
            p.setClipping(False)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(28, 28, 38))
            p.drawRoundedRect(QRectF(_PAD, cy, _COVER, _COVER), _CR, _CR)
        if self._strip_a > 0.01 and self._queue and not self._cover_only:
            nxt = (self._queue.get('next') or [])[:_N_NEXT]
            prv = list(reversed((self._queue.get('prev') or [])[-_N_PREV:]))
            for i, t in enumerate(nxt):
                self._draw_tile(p, ccx, cy + _COVER + _TILE_GAP + i * _TILE_PITCH, self._cover(t.get('art'), _TILE), self._strip_a * max(0.18, 0.62 - i * 0.14))
            for i, t in enumerate(prv):
                self._draw_tile(p, ccx, cy - _TILE_GAP - _TILE - i * _TILE_PITCH, self._cover(t.get('art'), _TILE), self._strip_a * max(0.18, 0.62 - i * 0.14))
            p.save()
            p.resetTransform()
            p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            s = self._scale or 1.0
            up_px = int(_N_PREV * _TILE_PITCH * s)
            dn_px = int(_N_NEXT * _TILE_PITCH * s)
            w, h = (self.width(), self.height())
            gt = QLinearGradient(0, 0, 0, up_px)
            gt.setColorAt(0.0, QColor(0, 0, 0, 255))
            gt.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(0, 0, w, up_px, gt)
            gb = QLinearGradient(0, h - dn_px, 0, h)
            gb.setColorAt(0.0, QColor(0, 0, 0, 0))
            gb.setColorAt(1.0, QColor(0, 0, 0, 255))
            p.fillRect(0, h - dn_px, w, dn_px, gb)
            p.restore()
        if self._hint_a > 0.01 and not self._compact:
            p.setOpacity(min(1.0, self._hint_a) * 0.92)
            p.setFont(QFont(_FONT, 11))
            p.setPen(QColor('#b4b4b2'))
            hy = cy + _COVER + _TILE_GAP + 4
            p.drawText(QRectF(0, hy, _W, 20), Qt.AlignHCenter | Qt.AlignVCenter, '↕  Scroll to browse')
            p.setOpacity(1.0)
        bx, by = (_PAD + 8, cy + _COVER - 8)
        if self._muted:
            self._badge(p, bx, by, _BADGE_R)
            self._mute_glyph(p, bx, by)
            return None
        if not self._playing:
            self._badge(p, bx, by, _BADGE_R)
            self._pause_glyph(p, bx, by)
        elif self._play_a > 0.02:
            p.setOpacity(self._play_a)
            self._badge(p, bx, by, _BADGE_R)
            self._play_glyph(p, bx, by)
            p.setOpacity(1.0)
        if self._safe:
            lx, ly = (_PAD + _COVER - 8, cy + 8)
            self._badge(p, lx, ly, _BADGE_R)
            self._lock_icon(p, lx, ly)
        if self._text_a <= 0.01:
            return None
        p.setOpacity(self._text_a)
        ct = (card_cur.get('title') if card_cur else None) or self._title or 'No track'
        ca = (card_cur.get('artist') if card_cur else None) or self._artist or ''
        tx = _PAD + _COVER + 16
        tw = self._text(p, tx, 54, ct, _WHITE, 15, bold=True, maxw=_W - tx - _PAD)
        aw = self._text(p, tx, 81, ca, _DIM, 11, bold=True, maxw=_W - tx - _PAD)
        if not self._move_mode:
            if self._hover_region == 'title':
                p.setPen(QPen(_WHITE, 1.0))
                p.drawLine(int(tx), 58, int(tx + tw), 58)
            elif self._hover_region == 'artist' and ca:
                p.setPen(QPen(_DIM, 1.0))
                p.drawLine(int(tx), 85, int(tx + aw), 85)
        spk_y = 104
        a = self._slider_a
        bx0, bx1, bh, bly = (tx + 30, _W - _PAD - 50, 4, spk_y - 2)
        self._soft_shadow(p, tx, spk_y - 7, 18, 14, 4)
        if a > 0.02:
            self._soft_shadow(p, bx0, bly, bx1 - bx0, bh, 2)
        self._speaker(p, tx, spk_y, self._volume)
        if a > 0.02:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(55 * a)))
            p.drawRoundedRect(QRectF(bx0, bly, bx1 - bx0, bh), 2, 2)
            if self._vol > 0.01:
                p.setBrush(QColor(255, 255, 255, int(235 * a)))
                p.drawRoundedRect(QRectF(bx0, bly, (bx1 - bx0) * self._vol, bh), 2, 2)
