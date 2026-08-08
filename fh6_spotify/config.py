import dataclasses
import json
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    port: int = 5300
    full_level: float = 0.7
    full_level_user_set: bool = False
    duck_level: float = 0.15
    idle_level: float = 0.3
    idle_when_stopped: bool = False
    idle_speed_threshold: float = 2.0
    idle_after_stationary_s: float = 3.0
    menu_level: float = 0.1
    unfocused_level: float = 0.3
    mute_level: float = 0.0
    volume_ramp_in: float = 0.115
    volume_ramp_out: float = 0.3
    ducking_enabled: bool = True
    duck_scope: str = "game"
    duck_on_own_voice: bool = False
    convo_window_s: float = 6.0
    mic_device: str = ""
    vad_aggressiveness: int = 3
    vad_threshold: float = 0.35
    low_cpu_mode: bool = False
    hangover_ms: int = 600
    debounce_ms: int = 150
    telemetry_timeout_s: float = 2.0
    spotify_process_name: str = "Spotify.exe"
    source: str = "spotify"
    browser_process_names: tuple = (
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "operagx.exe",
        "vivaldi.exe",
        "librewolf.exe",
        "chromium.exe",
        "arc.exe",
        "zen.exe",
        "waterfox.exe",
        "floorp.exe",
        "palemoon.exe",
        "yandex.exe",
        "whale.exe",
        "thorium.exe",
        "tor.exe",
        "mullvadbrowser.exe",
        "duckduckgo.exe",
    )
    applemusic_process_names: tuple = (
        "AMPLibraryAgent.exe",
        "AppleMusic.exe",
        "Apple Music.exe",
    )
    localmedia_process_names: tuple = (
        "vlc.exe",
        "wmplayer.exe",
        "Microsoft.Media.Player.exe",
        "foobar2000.exe",
        "MusicBee.exe",
        "AIMP.exe",
        "winamp.exe",
        "musikcube.exe",
        "Dopamine.exe",
        "mpc-hc64.exe",
        "mpc-hc.exe",
        "mpc-be64.exe",
        "mpc-be.exe",
    )
    tidal_process_names: tuple = ("TIDAL.exe", "TIDALPlayer.exe")
    amazonmusic_process_names: tuple = ("Amazon Music.exe",)
    ytmusic_process_names: tuple = (
        "YouTube Music.exe",
        "YouTube Music Desktop App.exe",
        "youtube-music.exe",
        "ytmd.exe",
        "youtube-music-desktop-app.exe",
    )
    game_process_name: str = "forzahorizon6.exe"
    mode: str = "forza"
    general_target_process: str = ""
    game_preset: str = "forza"
    game_preset_chosen: bool = False
    auto_detect_game: bool = True
    isolation_mode: str = "default_output"
    gamepad_skip_enabled: bool = True
    skip_min_speed: float = 3.0
    latch_release_speed: float = 0.556
    share_latch_hold_s: float = 2.5
    latch_snap_from_speed: float = 10.0
    latch_release_accel: int = 10
    latch_sustain_s: float = 0.1
    travel_jump_m: float = 500.0
    controller_idle_poll_off_s: float = 30.0
    overlay_screen: str = ""
    telemetry_host: str = "0.0.0.0"
    telemetry_forward: str = ""
    visualizer_layout: str = ""
    visualizer_gpu: bool = True
    visualizer_intro_seen: bool = False
    visualizer_fog_energy: float = 1.0
    visualizer_system_audio: bool = False
    safe_mode_button: str = "micBtn"
    open_button: str = "micBtn"
    open_trigger: str = ""
    open_hotkey: str = "key:16+17+18+83"
    open_hold_ms: int = 1200
    safe_mode_default: bool = False
    pause_button: str = ""
    skip_button: str = ""
    latch_button: str = "square"
    ui_scale: float = 1.25
    theme: str = "dark"
    last_seen_version: str = ""
    input_device: str = "playstation"
    wheel_backend: str = "hid"
    device_chosen: bool = False
    forza_gate_seen: bool = False
    rl_gate_seen: bool = False
    other_gate_seen: bool = False
    tour_done: bool = False
    tour_reset_migration_done: bool = False
    intro_reset_migration_v2_done: bool = False
    bindings: dict = field(default_factory=dict)
    hold_actions: list = field(default_factory=list)
    bindings_by_device: dict = field(default_factory=dict)
    hold_actions_by_device: dict = field(default_factory=dict)
    bind_hold_ms: int = 300
    vol_step: float = 0.05
    vol_hold_sensitivity: float = 1.0
    mouse_control_enabled: bool = False
    mouse_modifier: str = "forward"
    mouse_music_actions: bool = True
    keyboard_summon: bool = False
    skip_menu_suppress_ms: int = 3000
    touchpad_volume_enabled: bool = True
    touchpad_skip_enabled: bool = True
    touchpad_sensitivity: float = 0.0011
    swipe_skip_threshold: int = 240
    touchpad_tap_enabled: bool = True
    pause_input: str = "tap"
    tap_max_ms: int = 250
    tap_move_threshold: int = 50
    tap_sensitivity: int = 70
    dpad_up_button: int = 11
    dpad_down_button: int = 12
    dpad_left_button: int = 13
    dpad_right_button: int = 14
    skip_resume_buttons: tuple = (0, 1, 2, 3)
    overlay_enabled: bool = True
    overlay_position: str = "middle_left"
    overlay_custom_x: float = -1.0
    overlay_custom_y: float = -1.0
    overlay_scale: float = 1.0
    overlay_compact: bool = False
    overlay_always_on: bool = False
    overlay_in_game_only: bool = False
    overlay_drive_only: bool = False
    stream_overlay: bool = False
    stream_overlay_port: int = 7345
    overlay_video: bool = False
    canvas_service_port: int = 7355
    connect_skip: bool = False
    overlay_preset: dict = field(default_factory=dict)
    overlay_presets: dict = field(default_factory=dict)
    overlay_preset_name: str = ""
    close_to_tray: bool = True
    tray_hint_seen: bool = False
    skip_quit_confirm: bool = False
    custom_process_names: tuple = ()
    custom_smtc_match: str = ""
    custom_label: str = ""
    custom_icon_path: str = ""
    debug: bool = False
    demo_mode: bool = False

    @classmethod
    def load(cls, path: str) -> "Config":
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cls()
        valid = {f.name for f in dataclasses.fields(cls)}
        data = {k: v for k, v in raw.items() if k in valid}
        try:
            return cls(**data)
        except TypeError:
            return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(self), f, indent=2)
        os.replace(tmp, path)

    def apply_from(self, other: "Config") -> None:
        for fld in dataclasses.fields(self):
            setattr(self, fld.name, getattr(other, fld.name))


def default_config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Segue", "config.json")
