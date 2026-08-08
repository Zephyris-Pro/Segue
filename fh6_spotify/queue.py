"""Poll the local canvas_service /queue into ui[\'np_queue\'] while the mouse music\nmodifier is held, for the overlay\'s prev/next strip.\n\nReuses the same local service + librespot session as Canvas (scripts/canvas_service.py,\ncanvas.py). Best-effort: not held -> np_queue cleared; service down -> last value kept.\nOnly worth running when the service is up (overlay_video), since that\'s what serves\n/queue. The poll is local + cheap (the service holds the live Connect cluster), so a\nfew Hz while held is fine.\n"""

from __future__ import annotations
import json
import threading
import time
import urllib.request

_started = False


def start_queue_poller(ui: dict, get_port) -> None:
    """Start the background poller once. get_port() -> canvas_service port."""
    global _started
    if _started:
        return
    else:
        _started = True

        def loop():
            last_active = 0.0
            while True:
                try:
                    active = ui.get("mouse_held") or ui.get("ovl_hover")
                    if active:
                        last_active = time.monotonic()
                    if active or time.monotonic() - last_active < 2.5:
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{get_port()}/queue",
                            headers={"User-Agent": "Segue"},
                        )
                        with urllib.request.urlopen(req, timeout=2) as r:
                            ui["np_queue"] = json.loads(r.read())
                        time.sleep(0.2 if active else 0.15)
                    else:
                        if ui.get("np_queue") is not None:
                            ui["np_queue"] = None
                        time.sleep(0.12)
                except Exception:
                    time.sleep(0.5)

        threading.Thread(target=loop, daemon=True, name="segue-queue-poll").start()
