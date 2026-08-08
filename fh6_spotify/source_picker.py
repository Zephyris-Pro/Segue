"""Custom music source resolution for Segue.\n\nTurns the live system state into the three config values the custom source\nneeds: the audio process(es) to duck (volume), the SMTC app-id substring to\nmatch (track info / transport), and a friendly label. The user picks a PLAYING\napp from list_audio_apps(); we read its exact process from the live WASAPI\nsession (so hidden child processes are captured correctly) and best-effort\nmatch its SMTC session for track info.\n"""

from collections import namedtuple

AudioApp = namedtuple("AudioApp", "exe display path")
AudioApp.__new__.__defaults__ = ("",)


def _exe_base(exe: str) -> str:
    """\'Cider.exe\' -> \'cider\' (lowercased, .exe stripped)."""
    return (exe or "").lower().rsplit(".exe", 1)[0]


def match_smtc(exe: str, app_ids) -> str:
    """Best SMTC app-id substring for an exe, or \"\" when none matches. Matches\n    when the exe base name appears in an AUMID (case-insensitive) - that\'s the\n    substring runner._src_match will test against future app-ids."""
    base = _exe_base(exe)
    if not base:
        return ""
    else:
        for aid in app_ids:
            if base in (aid or "").lower():
                return base
        return ""


def resolve_pick(app: AudioApp, smtc_app_ids) -> dict:
    """Config values for a picked, currently-playing app."""
    return {
        "custom_process_names": (app.exe,),
        "custom_smtc_match": match_smtc(app.exe, smtc_app_ids),
        "custom_label": (app.display or "").strip() or _exe_base(app.exe) or "Custom",
        "custom_icon_path": getattr(app, "path", "") or "",
    }


def builtin_source_exes(config) -> set:
    """Lowercased exe names already covered by a built-in source (Spotify,\n    browser, Apple/Amazon/YT Music, TIDAL, local players). Excluded from the\n    custom picker - those have dedicated source entries, so e.g. Spotify\n    shouldn\'t also appear under Custom. Reuses SpotifyVolume._candidates so the\n    name lists stay in one place."""
    from fh6_spotify.spotify_volume import SpotifyVolume

    sv = SpotifyVolume(config)
    names = set()
    for src in [
        "spotify",
        "browser",
        "applemusic",
        "localmedia",
        "tidal",
        "amazonmusic",
        "ytmusic",
    ]:
        for n in sv._candidates(src):
            names.add((n or "").lower())
    names.discard("")
    return names


def _session_peak(s):
    """Instantaneous output level (0..1) of an audio session via its peak meter,\n    or None if it can\'t be read. Lets us list only apps ACTUALLY making sound."""
    try:
        from pycaw.pycaw import IAudioMeterInformation

        return float(s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
    except Exception:
        return None


def list_audio_apps(exclude=None):
    """Processes holding a WASAPI audio session, as AudioApp rows (deduped by\n    exe). Reading the REAL session process means hidden children\n    (youtube-music-desktop-app.exe, TIDALPlayer.exe, AMPLibraryAgent.exe) are\n    captured exactly, no guessing.\n\n    Filtered to apps OUTPUTTING sound right now (peak meter > 0) and sorted\n    loudest-first, so idle sessions (Discord, Steam, a dictation tool, the game)\n    never appear. Returns empty when nothing is audible - the dialog shows a\n    \"start your music\" hint instead of listing silent apps. `exclude`\n    (lowercased exe names) drops apps already covered by a built-in source - see\n    builtin_source_exes."""
    from pycaw.pycaw import AudioUtilities

    skip = {e.lower() for e in exclude or ()}
    rows, seen = ([], set())
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return []
    for s in sessions:
        try:
            if not s.Process:
                continue
            else:
                exe = s.Process.name()
                if not exe or exe.lower() in seen or exe.lower() in skip:
                    continue
                else:
                    seen.add(exe.lower())
                    try:
                        disp = (s.DisplayName or "").strip()
                    except Exception:
                        disp = ""
                    try:
                        path = s.Process.exe()
                    except Exception:
                        path = ""
                    peak = _session_peak(s)
                    rows.append(
                        (peak if peak is not None else 0.0, AudioApp(exe, disp, path))
                    )
        except Exception:
            pass
    audible = [r for r in rows if r[0] > 0.001]
    audible.sort(key=lambda r: r[0], reverse=True)
    return [app for _, app in audible]


def list_smtc_app_ids():
    """Current SMTC session AUMIDs (apps that have a media session). Used to\n    best-effort match the picked app\'s track-info session. Empty on any\n    failure (track info just won\'t be available for the custom source)."""
    import asyncio

    async def _go():
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Mgr,
        )

        mgr = await Mgr.request_async()
        ids = []
        for s in mgr.get_sessions():
            try:
                ids.append(s.source_app_user_model_id or "")
            except Exception:
                pass
            else:
                pass
        return ids

    try:
        return asyncio.run(_go())
    except Exception:
        return []
