"""Send Windows global media keys. Spotify responds to these by default."""

import ctypes

_VK_MEDIA_NEXT_TRACK = 176
_VK_MEDIA_PREV_TRACK = 177
_VK_MEDIA_PLAY_PAUSE = 179
_KEYEVENTF_KEYUP = 2


def _tap(vk: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def media_next() -> None:
    _tap(_VK_MEDIA_NEXT_TRACK)


def media_prev() -> None:
    _tap(_VK_MEDIA_PREV_TRACK)


def media_playpause() -> None:
    _tap(_VK_MEDIA_PLAY_PAUSE)
