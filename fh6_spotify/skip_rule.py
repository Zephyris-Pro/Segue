from fh6_spotify.config import Config


class SkipRule:
    """Decide what a D-pad press should do to Spotify.\n\n    Right -> next track, Left -> previous track (both only when driving and not\n    suppressed). Down opens an in-game menu (ANNA/LINK) and starts a suppression\n    window; while it runs, Right/Left are treated as menu navigation, not skips.\n    Up is ignored.\n"""

    SUPPRESS_DIRS = ("down",)

    def __init__(self, config: Config):
        self.c = config
        self._suppressed_until = 0.0

    def on_resume(self) -> None:
        """A menu item was picked -> menu closed -> end suppression now."""
        self._suppressed_until = 0.0

    def on_comms(self, now: float) -> None:
        """A comms wheel (ANNA/LINK) was opened via a button OTHER than D-pad\n        Down - e.g. Left-stick click (L3) bound to LINK in Forza. Start the\n        same skip-lock window so navigating the wheel with D-pad Left/Right\n        doesn\'t skip tracks. Clears early via on_resume() when an option is\n        picked, exactly like the D-pad Down path."""
        self._suppressed_until = now + self.c.skip_menu_suppress_ms / 1000

    def on_dpad(self, direction: str, is_driving: bool, now: float) -> str | None:
        """Return \"next\"/\"prev\" to skip, or None. `direction` in up/down/left/right."""
        if direction in self.SUPPRESS_DIRS:
            self.on_comms(now)
            return
        else:
            if direction in ["right", "left"]:
                if not is_driving:
                    return
                else:
                    if now < self._suppressed_until:
                        return
                    else:
                        return "next" if direction == "right" else "prev"
            else:
                return None
