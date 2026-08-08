"""Windows taskbar thumbnail-toolbar buttons (prev / play-pause / next).

Spotify shows these when you hover its taskbar icon. Qt6 dropped QtWinExtras
(QWinThumbnailToolBar), so this talks to ITaskbarList3 directly via ctypes.

Flow (all on the GUI/main thread, which is COM-STA):
  - The shell creates the window's taskbar button asynchronously and then posts a
    registered "TaskbarButtonCreated" message. ONLY after that may ThumbBarAddButtons
    be called -> the host watches for that message and calls :meth:`add`.
  - A thumb-button click arrives as WM_COMMAND with HIWORD(wParam)==THBN_CLICKED and
    LOWORD(wParam)==the button id -> :meth:`handle_command` routes it.
  - :meth:`set_playing` swaps the middle icon (play <-> pause) via ThumbBarUpdateButtons.

Best-effort: every COM/GDI call is guarded; any failure just means no buttons.
"""

from __future__ import annotations
import ctypes
import sys
import uuid
from ctypes import POINTER, Structure, byref, c_void_p
from ctypes import wintypes

BTN_PREV, BTN_PLAYPAUSE, BTN_NEXT = (1, 2, 3)
_THB_ICON, _THB_TOOLTIP, _THB_FLAGS = (2, 4, 8)
_THBF_ENABLED = 0
_THBN_CLICKED = 6144
_WM_COMMAND = 273
_CLSCTX_INPROC_SERVER = 1


class _GUID(Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(s: str) -> _GUID:
    g = _GUID()
    ctypes.memmove(byref(g), uuid.UUID(s).bytes_le, 16)
    return g


class _THUMBBUTTON(Structure):
    _fields_ = [
        ("dwMask", ctypes.c_uint32),
        ("iId", ctypes.c_uint32),
        ("iBitmap", ctypes.c_uint32),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 260),
        ("dwFlags", ctypes.c_uint32),
    ]


class _ICONINFO(Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", ctypes.c_uint32),
        ("yHotspot", ctypes.c_uint32),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class _BMIH(Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPPM", ctypes.c_int32),
        ("biYPPM", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


_CLSID_TaskbarList = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_ITaskbarList3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"


def _qimage_to_hicon(img):
    """Convert a QImage to an HICON (32-bit ARGB) via a top-down DIB section.
    Returns 0 on failure. (restype/argtypes set so 64-bit handles aren't truncated.)"""
    try:
        from PySide6.QtGui import QImage

        img = img.convertToFormat(QImage.Format_ARGB32)
        w, h = (img.width(), img.height())
        gdi = ctypes.windll.gdi32
        user = ctypes.windll.user32
        user.GetDC.restype = c_void_p
        user.GetDC.argtypes = [c_void_p]
        user.ReleaseDC.argtypes = [c_void_p, c_void_p]
        gdi.CreateDIBSection.restype = c_void_p
        gdi.CreateDIBSection.argtypes = [
            c_void_p,
            c_void_p,
            ctypes.c_uint,
            POINTER(c_void_p),
            c_void_p,
            ctypes.c_uint,
        ]
        gdi.CreateBitmap.restype = c_void_p
        gdi.CreateBitmap.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            c_void_p,
        ]
        gdi.DeleteObject.argtypes = [c_void_p]
        user.CreateIconIndirect.restype = c_void_p
        user.CreateIconIndirect.argtypes = [c_void_p]
        bi = _BMIH()
        bi.biSize = ctypes.sizeof(_BMIH)
        bi.biWidth = w
        bi.biHeight = -h
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        ppv = c_void_p()
        hdc = user.GetDC(None)
        hbm_color = gdi.CreateDIBSection(hdc, byref(bi), 0, byref(ppv), None, 0)
        user.ReleaseDC(None, hdc)
        if not hbm_color or not ppv:
            return 0
        data = bytes(img.constBits())[: w * h * 4]
        ctypes.memmove(ppv, data, len(data))
        mask_stride = (w + 15) // 16 * 2
        mask_buf = (ctypes.c_char * (mask_stride * h))()
        hbm_mask = gdi.CreateBitmap(w, h, 1, 1, mask_buf)
        info = _ICONINFO(True, 0, 0, hbm_mask, hbm_color)
        hicon = user.CreateIconIndirect(byref(info))
        gdi.DeleteObject(hbm_color)
        gdi.DeleteObject(hbm_mask)
        return hicon or 0
    except Exception:
        return 0


def _glyph(kind: str, size: int = 40):
    """White transport glyph matching Segue's own transport icons (see
    settings._media_glyph): single triangle + bar for prev/next, not double.
    Drawn on Segue's 40-unit canvas, enlarged ~1.38x so it fills the thumb button."""
    from PySide6.QtGui import QImage, QPainter, QColor, QPainterPath, QBrush
    from PySide6.QtCore import QRectF, Qt

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 40.0, size / 40.0)
    p.translate(20, 20)
    p.scale(1.38, 1.38)
    p.translate(-20, -20)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(255, 255, 255)))
    if kind == "play":
        path = QPainterPath()
        path.moveTo(15, 11)
        path.lineTo(15, 29)
        path.lineTo(30, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == "pause":
        p.drawRoundedRect(QRectF(12.5, 9.75, 6, 20.5), 2.4, 2.4)
        p.drawRoundedRect(QRectF(21.5, 9.75, 6, 20.5), 2.4, 2.4)
    elif kind == "next":
        path = QPainterPath()
        path.moveTo(13, 11)
        path.lineTo(13, 29)
        path.lineTo(26, 20)
        path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(27, 11, 4, 18), 1.5, 1.5)
    elif kind == "prev":
        path = QPainterPath()
        path.moveTo(27, 11)
        path.lineTo(27, 29)
        path.lineTo(14, 20)
        path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(9, 11, 4, 18), 1.5, 1.5)
    p.end()
    return img


class TaskbarButtons:
    """Manages the 3 thumbnail buttons for a window. `on_action` is called with
    'prev' | 'playpause' | 'next' when a button is clicked."""

    def __init__(self, hwnd: int, on_action):
        self._hwnd = int(hwnd)
        self._on_action = on_action
        self._tbl = None
        self._added = False
        self._playing = False
        self._icons = {}
        try:
            self.msg_created = ctypes.windll.user32.RegisterWindowMessageW(
                "TaskbarButtonCreated"
            )
        except Exception:
            self.msg_created = 0

    def _call(self, index, restype, argtypes, *args):
        """Invoke the ITaskbarList3 vtable method at `index`."""
        vtbl = ctypes.cast(self._tbl, POINTER(c_void_p))[0]
        fn = ctypes.cast(vtbl, POINTER(c_void_p))[index]
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        return proto(fn)(self._tbl, *args)

    def _ensure(self):
        if self._tbl is not None:
            return True
        if sys.platform != "win32":
            return False
        try:
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)
            ptr = c_void_p()
            hr = ole32.CoCreateInstance(
                byref(_guid(_CLSID_TaskbarList)),
                None,
                _CLSCTX_INPROC_SERVER,
                byref(_guid(_IID_ITaskbarList3)),
                byref(ptr),
            )
            if hr != 0 or not ptr:
                return False
            self._tbl = ptr
            self._call(3, ctypes.c_long, [])
            return True
        except Exception:
            self._tbl = None
            return False

    def _icon(self, kind):
        if kind not in self._icons:
            self._icons[kind] = _qimage_to_hicon(_glyph(kind))
        return self._icons[kind]

    def _mk(self, bid, kind, tip):
        b = _THUMBBUTTON()
        b.dwMask = _THB_ICON | _THB_TOOLTIP | _THB_FLAGS
        b.iId = bid
        b.hIcon = self._icon(kind)
        b.szTip = tip
        b.dwFlags = _THBF_ENABLED
        return b

    def _buttons(self):
        arr = (_THUMBBUTTON * 3)()
        arr[0] = self._mk(BTN_PREV, "prev", "Previous")
        arr[1] = self._mk(
            BTN_PLAYPAUSE,
            "pause" if self._playing else "play",
            "Pause" if self._playing else "Play",
        )
        arr[2] = self._mk(BTN_NEXT, "next", "Next")
        return arr

    def add(self, src=""):
        """Add the buttons (call after the TaskbarButtonCreated message)."""
        try:
            if self._added:
                return
            if not self._ensure():
                return
            arr = self._buttons()
            hr = self._call(
                15,
                ctypes.c_long,
                [wintypes.HWND, ctypes.c_uint, POINTER(_THUMBBUTTON)],
                self._hwnd,
                3,
                arr,
            )
            self._added = hr == 0
        except Exception:
            return None

    def set_playing(self, playing: bool):
        try:
            if bool(playing) == self._playing:
                return
            self._playing = bool(playing)
            if not self._added:
                return
            arr = self._buttons()
            self._call(
                16,
                ctypes.c_long,
                [wintypes.HWND, ctypes.c_uint, POINTER(_THUMBBUTTON)],
                self._hwnd,
                3,
                arr,
            )
        except Exception:
            return None

    def handle_command(self, wparam: int) -> bool:
        """Route a WM_COMMAND. Returns True if it was a thumb-button click."""
        if wparam >> 16 & 65535 != _THBN_CLICKED:
            return False
        kind = {BTN_PREV: "prev", BTN_PLAYPAUSE: "playpause", BTN_NEXT: "next"}.get(
            wparam & 65535
        )
        if kind:
            try:
                self._on_action(kind)
            except Exception:
                return True
        return True
