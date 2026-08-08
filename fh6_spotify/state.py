from fh6_spotify.config import Config


class StateMachine:
    def __init__(self, config: Config):
        self.c = config
        self.committed = config.full_level
        self._pending = None
        self._pending_since = 0.0
        self._stationary_since = None

    def _desired(
        self,
        is_race_on: bool | None,
        speech: bool,
        speed: float | None,
        now: float,
        is_focused: bool | None = None,
        is_running: bool = True,
    ) -> float:
        base = self._base_desired(
            is_race_on, speech, speed, now, is_focused, is_running
        )
        if (
            getattr(self.c, "duck_scope", "game") == "system"
            and speech
            and self.c.ducking_enabled
        ):
            return min(base, self.c.duck_level)
        else:
            return base

    def _base_desired(
        self,
        is_race_on: bool | None,
        speech: bool,
        speed: float | None,
        now: float,
        is_focused: bool | None = None,
        is_running: bool = True,
    ) -> float:
        if self.c.mode == "general":
            if is_focused is False:
                return self.c.unfocused_level
            else:
                if speech and self.c.ducking_enabled:
                    return self.c.duck_level
                else:
                    return self.c.full_level
        else:
            if is_race_on is None:
                return self.c.menu_level if is_running else self.c.full_level
            else:
                if not is_race_on:
                    return self.c.menu_level
                else:
                    if (
                        self.c.idle_when_stopped
                        and speed is not None
                        and (speed < self.c.idle_speed_threshold)
                    ):
                        if self._stationary_since is None:
                            self._stationary_since = now
                        stationary = (
                            now - self._stationary_since
                            >= self.c.idle_after_stationary_s
                        )
                    else:
                        self._stationary_since = None
                        stationary = False
                    if speech and self.c.ducking_enabled:
                        return self.c.duck_level
                    else:
                        if stationary:
                            return self.c.idle_level
                        else:
                            return self.c.full_level

    def update(
        self,
        is_race_on: bool | None,
        speech: bool,
        now: float,
        speed: float | None = None,
        is_focused: bool | None = None,
        is_running: bool = True,
    ) -> float:
        desired = self._desired(is_race_on, speech, speed, now, is_focused, is_running)
        if desired == self.committed:
            self._pending = None
            return self.committed
        else:
            if desired != self._pending:
                self._pending = desired
                self._pending_since = now
            if self._pending is not None:
                elapsed_ms = (now - self._pending_since) * 1000
                if elapsed_ms >= self.c.debounce_ms:
                    self.committed = self._pending
                    self._pending = None
            return self.committed
