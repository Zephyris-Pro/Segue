"""Segue entry point (source build).

Mirrors the original PyInstaller __main__, minus the frozen-only splash screen.
  --restore-volumes : restore any Spotify volumes left dirty by a hard exit, then quit
  --watch           : run the headless watchdog loop
  (default)         : set per-monitor DPI awareness, then launch the unified app
"""

import sys

if __name__ == "__main__":
    if "--restore-volumes" in sys.argv:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            from fh6_spotify.spotify_volume import restore_dirty_volumes

            restore_dirty_volumes()
        except Exception:
            pass
        sys.exit(0)

    if "--watch" in sys.argv:
        from fh6_spotify.config import Config, default_config_path
        from fh6_spotify.watch import watch

        watch(Config.load(default_config_path()))
    else:
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
        from fh6_spotify.app import main

        main()
