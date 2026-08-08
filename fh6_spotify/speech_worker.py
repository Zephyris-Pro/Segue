"""Background thread: capture the game's audio (only) and run voice-activity
detection, exposing a `speech_active` flag. Decoupled from the radio loop so
audio reads never stall it. All COM/capture work stays on this one thread.
"""

import threading
import time
from fh6_spotify.config import Config
from fh6_spotify.speech import SpeechDetector


class SpeechWorker:
    def __init__(self, config: Config):
        self.c = config
        self._other_active = False
        self._own_active = False
        self._convo_until = 0.0
        self.cpu_pct = 0.0
        self._stop = False
        self._paused = False
        self.game_running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._mic_thread = threading.Thread(target=self._mic_run, daemon=True)

    @property
    def speech_active(self) -> bool:
        """The duck signal: a friend is talking, OR you are talking mid-convo."""
        return self._other_active or self._own_active

    def _music_source_proc(self):
        """Image name of the running music-source process to EXCLUDE from system
        capture (so the song's own audio doesn't self-trigger the ducker). None
        if the source app isn't running."""
        src = self.c.source
        if src == "browser":
            cands = self.c.browser_process_names
        elif src == "applemusic":
            cands = self.c.applemusic_process_names
        elif src == "localmedia":
            cands = self.c.localmedia_process_names
        elif src == "tidal":
            cands = getattr(self.c, "tidal_process_names", ("TIDAL.exe",))
        elif src == "amazonmusic":
            cands = getattr(self.c, "amazonmusic_process_names", ("Amazon Music.exe",))
        elif src == "ytmusic":
            cands = getattr(self.c, "ytmusic_process_names", ("YouTube Music.exe",))
        else:
            cands = (self.c.spotify_process_name,)
        try:
            import psutil

            running = {
                (p.info.get("name") or "").lower()
                for p in psutil.process_iter(["name"])
            }
        except Exception:
            return None
        for c in cands:
            if c and c.lower() in running:
                return c
        return None

    def _discord_proc(self):
        """Image name of a running Discord build, or None. Used by system
        scope's no-game fallback: with the game closed we capture ONLY
        Discord, so friends always duck."""
        try:
            import psutil

            running = {
                (p.info.get("name") or "").lower()
                for p in psutil.process_iter(["name"])
            }
        except Exception:
            return None
        for cand in ["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe"]:
            if cand.lower() in running:
                return cand
        return None

    def set_paused(self, paused: bool) -> None:
        """Pause/resume the capture+VAD loop. Pausing makes the thread sleep
        instead of reading audio - lets the caller drop ~all CPU while music
        is already at menu volume anyway.

        System scope listens to ALL apps, so the race-state CPU-saver pause is
        ignored there - it stays listening."""
        if getattr(self.c, "duck_scope", "game") == "system":
            paused = False
        new = bool(paused)
        if new == self._paused:
            return
        self._paused = new
        if new:
            self._other_active = False
            self.cpu_pct = 0.0

    def start(self) -> None:
        self._thread.start()
        self._mic_thread.start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        from fh6_spotify.proc_capture import ProcessLoopbackCapture

        classifier = None
        if classifier is None:
            try:
                from fh6_spotify.silero_vad import SileroClassifier

                classifier = SileroClassifier(self.c.vad_threshold)
            except Exception as exc:
                print(f"  Silero VAD load failed ({exc}); ducking inactive")
                return
        detector = SpeechDetector(self.c, classifier=classifier)
        while not self._stop:
            if self._paused:
                time.sleep(0.2)
                continue
            cap = None
            scope = getattr(self.c, "duck_scope", "game")
            game_up = bool(getattr(self, "game_running", True))
            cap_mode = None
            if scope == "system" and not game_up:
                dproc = self._discord_proc()
                if not dproc:
                    self._other_active = False
                    time.sleep(2.0)
                    continue
                try:
                    cap = ProcessLoopbackCapture(dproc)
                except Exception:
                    time.sleep(2.0)
                    continue
                cap_mode = "discord"
            if scope == "system" and cap is None:
                music = self._music_source_proc()
                if music:
                    try:
                        cap = ProcessLoopbackCapture(music, exclude=True)
                    except Exception:
                        time.sleep(2.0)
                        continue
                    cap_mode = "system"
            if cap is None:
                target = (
                    self.c.general_target_process
                    if self.c.mode == "general"
                    else self.c.game_process_name
                )
                if not target:
                    time.sleep(2.0)
                    continue
                try:
                    cap = ProcessLoopbackCapture(target)
                except Exception:
                    time.sleep(2.0)
                    continue
            win_start = time.monotonic()
            busy = 0.0
            try:
                for frame in cap.frames():
                    if self._stop or self._paused:
                        break
                    if scope == "system":
                        g = bool(getattr(self, "game_running", True))
                        if (
                            cap_mode == "system"
                            and not g
                            or (cap_mode == "discord" and g)
                        ):
                            break
                    if hasattr(classifier, "_thresh"):
                        classifier._thresh = self.c.vad_threshold
                    t0 = time.perf_counter()
                    _now = time.monotonic()
                    active = detector.feed(frame, _now)
                    self._other_active = active
                    if active:
                        self._convo_until = _now + getattr(
                            self.c, "convo_window_s", 6.0
                        )
                    busy += time.perf_counter() - t0
                    now = time.monotonic()
                    if now - win_start >= 1.0:
                        self.cpu_pct = busy / (now - win_start) * 100.0
                        busy = 0.0
                        win_start = now
            except Exception:
                pass
            finally:
                self._other_active = False
                self.cpu_pct = 0.0
                try:
                    cap.close()
                except Exception:
                    pass
            time.sleep(0.5)

    def _own_voice_on(self) -> bool:
        """True when the own-voice mic feature should be active: opted in AND in
        system scope (Include Discord) AND ducking enabled. Read live so toggling
        it in the UI arms/disarms without restarting the worker."""
        return (
            bool(getattr(self.c, "duck_on_own_voice", False))
            and getattr(self.c, "duck_scope", "game") == "system"
            and bool(self.c.ducking_enabled)
        )

    def _mic_run(self) -> None:
        """Own-voice path. Opens the mic ONLY while a conversation is active (a
        friend spoke within convo_window_s), runs VAD on your voice, and keeps the
        duck held during your turns by extending the conversation window."""
        cap = None
        classifier = None
        detector = None

        def _drop():
            nonlocal cap
            nonlocal detector
            nonlocal classifier
            self._own_active = False
            if cap is not None:
                try:
                    cap.close()
                except Exception:
                    pass
            cap = None
            classifier = None
            detector = None

        while not self._stop:
            if not self._own_voice_on() or time.monotonic() >= self._convo_until:
                if cap is not None:
                    _drop()
                time.sleep(0.1)
                continue
            if cap is None:
                try:
                    from fh6_spotify.mic_capture import MicCapture
                    from fh6_spotify.silero_vad import SileroClassifier

                    cap = MicCapture(device_name=getattr(self.c, "mic_device", ""))
                    classifier = SileroClassifier(self.c.vad_threshold)
                    detector = SpeechDetector(self.c, classifier=classifier)
                except Exception:
                    _drop()
                    time.sleep(1.0)
                    continue
            try:
                for frame in cap.frames():
                    if self._stop or not self._own_voice_on():
                        break
                    now = time.monotonic()
                    if now >= self._convo_until:
                        break
                    if hasattr(classifier, "_thresh"):
                        classifier._thresh = self.c.vad_threshold
                    spoke = bool(detector.feed(frame, now))
                    self._own_active = spoke
                    if spoke:
                        self._convo_until = now + getattr(self.c, "convo_window_s", 6.0)
            except Exception:
                pass
            finally:
                _drop()
