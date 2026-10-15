import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PureWindowsPath
from threading import Event
from types import SimpleNamespace

import pytest

from cs2hl import ffmpeg as ffmpeg_mod
from cs2hl.capture import hlae
from cs2hl.capture.base import CaptureCancelled, CaptureError, CaptureResult, CaptureSettings
from cs2hl.capture.hlae import (
    PRESET_NAME,
    PRESET_OPTIONS,
    HlaeBackend,
    HlaeConfig,
    build_boot_cfg,
    build_schedule,
    clip_problem,
    cs2_command_line,
    demo_stem,
    ffmpeg_preset_line,
    hlae_launch_args,
    hlae_process_env,
    is_settled,
    mux_command,
    record_name,
    select_clips,
    session_segments,
    split_sessions,
    stage_demo,
    steam_env,
    take_files,
    too_short,
)
from cs2hl.models import Clip, DemoMeta, Plan
from cs2hl.system import tools

ALICE = 76561198000000001
BOB = 76561198000000002

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

EXPECTED_BOOT_CFG = [
    "// Written by cs2hl for one recording session. Safe to delete.",
    "mirv_cvar_unhide_all",
    "engine_no_focus_sleep 0",
    r'mirv_streams settings add ffmpeg cs2hl_h264 "-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p -movflags +faststart {QUOTE}{AFX_STREAM_PATH}\\video.mp4{QUOTE}"',
    "mirv_streams record fps 60",
    "mirv_streams record screen enabled 1",
    "mirv_streams record screen settings cs2hl_h264",
    "mirv_streams record startMovieWav 1",
    "cl_hud_telemetry_frametime_show 0",
    "cl_hud_telemetry_net_misdelivery_show 0",
    "cl_hud_telemetry_ping_show 0",
    "cl_hud_telemetry_serverrecvmargin_graph_show 0",
    "cl_trueview_show_status 0",
    "r_show_build_info 0",
    "demo_ui_mode 0",
    "cl_draw_only_deathnotices 1",
    "mirv_deathmsg lifetime 10",
    "mirv_cmd clear",
    "mirv_cmd addAtTick 96 sv_cheats 1",
    "mirv_cmd addAtTick 96 cl_hud_telemetry_frametime_show 0",
    "mirv_cmd addAtTick 96 cl_hud_telemetry_net_misdelivery_show 0",
    "mirv_cmd addAtTick 96 cl_hud_telemetry_ping_show 0",
    "mirv_cmd addAtTick 96 cl_hud_telemetry_serverrecvmargin_graph_show 0",
    "mirv_cmd addAtTick 96 cl_trueview_show_status 0",
    "mirv_cmd addAtTick 96 r_show_build_info 0",
    "mirv_cmd addAtTick 96 demo_ui_mode 0",
    "mirv_cmd addAtTick 96 cl_draw_only_deathnotices 1",
    # clip001 segment 0 (1000-1300): seek 64 ticks (1 s) early, camera 32 ticks before the start
    "mirv_cmd addAtTick 96 demo_gototick 936",
    "mirv_cmd addAtTick 968 spec_mode 1",
    "mirv_cmd addAtTick 968 spec_player 3",
    "mirv_cmd addAtTick 968 mirv_deathmsg clear",
    "mirv_cmd addAtTick 968 mirv_deathmsg filter clear",
    "mirv_cmd addAtTick 968 mirv_deathmsg filter add block=1",
    f"mirv_cmd addAtTick 968 mirv_deathmsg filter add attackerMatch=x{ALICE} attackerIsLocal=1 block=0",
    r"mirv_cmd addAtTick 1000 mirv_streams record name ..\..\csgo\cs2hl\work\clip001_r03_s0",
    "mirv_cmd addAtTick 1000 mirv_streams record start",
    "mirv_cmd addAtTick 1300 mirv_streams record end",
    # clip001 segment 1 (1700-2000): same POV, no new kill-feed filter
    "mirv_cmd addAtTick 1301 demo_gototick 1636",
    "mirv_cmd addAtTick 1668 spec_mode 1",
    "mirv_cmd addAtTick 1668 spec_player 3",
    r"mirv_cmd addAtTick 1700 mirv_streams record name ..\..\csgo\cs2hl\work\clip001_r03_s1",
    "mirv_cmd addAtTick 1700 mirv_streams record start",
    "mirv_cmd addAtTick 2000 mirv_streams record end",
    # clip002 (5000-6000, no segments): new POV and kill feed
    "mirv_cmd addAtTick 2001 demo_gototick 4936",
    "mirv_cmd addAtTick 4968 spec_mode 1",
    "mirv_cmd addAtTick 4968 spec_player 5",
    "mirv_cmd addAtTick 4968 mirv_deathmsg clear",
    "mirv_cmd addAtTick 4968 mirv_deathmsg filter clear",
    "mirv_cmd addAtTick 4968 mirv_deathmsg filter add block=1",
    f"mirv_cmd addAtTick 4968 mirv_deathmsg filter add attackerMatch=x{BOB} attackerIsLocal=1 block=0",
    r"mirv_cmd addAtTick 5000 mirv_streams record name ..\..\csgo\cs2hl\work\clip002_r07_s0",
    "mirv_cmd addAtTick 5000 mirv_streams record start",
    "mirv_cmd addAtTick 6000 mirv_streams record end",
    "mirv_cmd addAtTick 6064 quit",
    "playdemo cs2hl/match",
]


def make_clip(clip_id, start, end, segments=(), slot=3, steamid=ALICE, name="alice"):
    return Clip(
        id=clip_id,
        round_num=1,
        pov_steamid=steamid,
        pov_name=name,
        start_tick=start,
        end_tick=end,
        start_s=start / 64,
        end_s=end / 64,
        score=10.0,
        labels=("3K",),
        tags=(),
        segments=tuple(segments),
        pov_slot=slot,
    )


def two_clip_plan_clips():
    a = make_clip("clip001_r03", 1000, 2000, segments=((1000, 1300), (1700, 2000)), slot=3)
    b = make_clip("clip002_r07", 5000, 6000, slot=5, steamid=BOB, name="bob")
    return a, b


def make_plan(*clips, tickrate=64.0):
    return Plan(meta=DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=tickrate), clips=list(clips))


def expand_like_hlae(options: str, stream_path: str) -> str:
    """Mirror of ReplaceAllW and the placeholder map in shared/AfxOutStreams.cpp (earliest match first)."""
    replacements = {
        "{AFX_STREAM_PATH}": stream_path,
        "{QUOTE}": '"',
        "\\{": "{",
        "\\}": "}",
        "\\\\": "\\",
    }
    text = options
    start = 0
    while True:
        best = None
        for key in sorted(replacements):
            pos = text.find(key, start)
            if pos != -1 and (best is None or pos < best[0]):
                best = (pos, key)
        if best is None:
            return text
        pos, key = best
        value = replacements[key]
        text = text[:pos] + value + text[pos + len(key):]
        start = pos + len(value)


def schedule_of(text: str) -> list[tuple[int, str]]:
    out = []
    for line in text.splitlines():
        m = re.fullmatch(r"mirv_cmd addAtTick (\d+) (.+)", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


# ---------------------------------------------------------------- boot cfg and schedule


def test_boot_cfg_for_two_clips_and_three_segments_is_exact():
    a, b = two_clip_plan_clips()
    assert build_boot_cfg([b, a], "match", 60, 64.0) == "\n".join(EXPECTED_BOOT_CFG) + "\n"


def test_schedule_is_in_tick_order_and_never_puts_the_camera_on_a_seek_tick():
    clips = [
        make_clip("c1", 3000, 4200, segments=((3000, 3300), (3600, 3700), (3705, 4200)), slot=2),
        make_clip("c2", 4210, 4500, slot=7, steamid=BOB),
        make_clip("c3", 90000, 91000, slot=None),
        make_clip("c4", 20000, 26000, segments=((20000, 21000), (25000, 26000)), slot=4),
    ]
    schedule = build_schedule(clips, 64.0)
    ticks = [t for t, _ in schedule]
    assert ticks == sorted(ticks)
    seek_ticks = {t for t, c in schedule if c.startswith("demo_gototick")}
    camera_ticks = {t for t, c in schedule if c.startswith("spec_")}
    assert seek_ticks and camera_ticks
    assert not seek_ticks & camera_ticks
    for (t1, c1), (t2, c2) in zip(schedule, schedule[1:]):
        if c1.startswith("demo_gototick"):
            assert int(c1.split()[1]) > t1  # seeks only go forward
    assert schedule[-1] == (91000 + 64, "quit")


def test_camera_and_kill_feed_come_before_each_record_start():
    a, b = two_clip_plan_clips()
    schedule = build_schedule([a, b], 64.0)
    starts = [t for t, c in schedule if c == "mirv_streams record start"]
    players = [(t, c) for t, c in schedule if c.startswith("spec_player")]
    assert len(starts) == 3 and len(players) == 3
    for (camera_tick, _), start in zip(players, starts):
        assert camera_tick < start
    filters = [t for t, c in schedule if "attackerMatch" in c]
    assert filters == [968, 4968]  # once per clip, at its first segment


def test_close_clips_play_through_and_switch_camera_after_the_previous_end():
    a = make_clip("a", 1000, 2000, slot=3)
    b = make_clip("b", 2010, 2500, slot=6, steamid=BOB)
    schedule = build_schedule([a, b], 64.0)
    assert [c for _, c in schedule if c.startswith("demo_gototick")] == ["demo_gototick 936"]
    assert (2001, "spec_player 6") in schedule
    assert (2010, "mirv_streams record start") in schedule


def test_no_camera_commands_without_a_slot_but_kill_feed_stays():
    text = build_boot_cfg([make_clip("a", 1000, 2000, slot=None)], "match", 60, 64.0)
    assert "spec_player" not in text and "spec_mode" not in text
    assert f"attackerMatch=x{ALICE}" in text


def test_clip_near_demo_start_is_not_seeked_and_ticks_stay_above_the_floor():
    schedule = build_schedule([make_clip("a", 150, 400, slot=1)], 64.0)
    assert not any(c.startswith("demo_gototick") for _, c in schedule)
    assert min(t for t, _ in schedule) == hlae.FIRST_TICK
    assert (150 - 32, "spec_player 1") in schedule


def test_schedule_uses_the_demo_tickrate():
    schedule = build_schedule([make_clip("a", 10000, 11000, slot=1)], 128.0)
    assert (96, "demo_gototick 9872") in schedule
    assert (9936, "spec_player 1") in schedule


def test_preset_line_matches_builtin_escaping():
    assert ffmpeg_preset_line() == f'mirv_streams settings add ffmpeg {PRESET_NAME} "{PRESET_OPTIONS}"'
    assert PRESET_OPTIONS.count("\\\\video.mp4") == 1
    assert not PRESET_NAME.lower().startswith("afx")


def test_preset_expands_to_single_backslash_output_path():
    expanded = expand_like_hlae(PRESET_OPTIONS, r"C:\cs2\game\csgo\cs2hl\work\clip001_r03_s0\take0000")
    assert expanded.endswith(r'"C:\cs2\game\csgo\cs2hl\work\clip001_r03_s0\take0000\video.mp4"')
    assert "-crf 16" in expanded and "-pix_fmt yuv420p" in expanded


def test_session_segments_and_clip_checks():
    a, b = two_clip_plan_clips()
    names = [s.name for s in session_segments([b, a])]
    assert names == ["clip001_r03_s0", "clip001_r03_s1", "clip002_r07_s0"]
    with pytest.raises(ValueError, match="overlaps"):
        session_segments([make_clip("x", 1000, 2000), make_clip("y", 1500, 2500)])
    assert "empty segment" in clip_problem(make_clip("x", 1000, 1000))
    assert "out of order" in clip_problem(make_clip("x", 1000, 2000, segments=((1500, 2000), (1000, 1200))))
    assert "clip id" in clip_problem(make_clip("bad id", 1000, 2000))
    assert clip_problem(a) is None


def test_select_clips_keeps_first_of_overlapping_group():
    a = make_clip("a", 1000, 2000)
    b = make_clip("b", 1500, 2500)
    c = make_clip("c", 2600, 3000)
    touching = make_clip("d", 3000, 3500)
    kept, skipped = select_clips([touching, c, b, a])
    assert [clip.id for clip in kept] == ["a", "c"]
    assert [clip.id for clip in skipped] == ["b", "d"]


def test_split_sessions_puts_overlapping_clips_in_later_sessions():
    a = make_clip("a", 0, 100)
    b = make_clip("b", 50, 150)
    c = make_clip("c", 200, 300)
    d = make_clip("d", 120, 220)
    assert [[m.id for m in batch] for batch in split_sessions([a, b, c, d])] == [["a", "d"], ["b", "c"]]
    assert [len(batch) for batch in split_sessions([make_clip("a", 0, 10), make_clip("b", 20, 30)])] == [2]


def test_split_sessions_uses_segment_span():
    # start_tick/end_tick disagree with the segments; the segments are what gets recorded
    a = make_clip("a", 0, 100, segments=((0, 50), (400, 500)))
    b = make_clip("b", 200, 300)
    assert [[m.id for m in batch] for batch in split_sessions([a, b])] == [["a"], ["b"]]


def test_record_name_rejects_spaces_and_quotes():
    assert record_name("clip001_r03_s0") == r"..\..\csgo\cs2hl\work\clip001_r03_s0"
    with pytest.raises(ValueError):
        record_name("clip 1")
    with pytest.raises(ValueError):
        record_name('clip"1')


def test_demo_stem_is_safe_for_console_paths():
    assert demo_stem(Path("My Match (1).dem")) == "My_Match__1_"
    assert demo_stem(Path("матч.dem")) == "____"
    assert demo_stem(Path("match.dem")) == "match"


# ---------------------------------------------------------------- launch


def test_cs2_command_line():
    assert cs2_command_line(2560, 1440) == "-steam -insecure -sw -w 2560 -h 1440 -nojoy +exec cs2hl_boot"


def test_hlae_launch_args_are_unattended_custom_loader():
    hlae_exe = PureWindowsPath(r"C:\Tools\HLAE\HLAE.exe")
    cs2_exe = PureWindowsPath(r"D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe")
    steam_dir = PureWindowsPath(r"C:\Program Files (x86)\Steam")
    args = hlae_launch_args(hlae_exe, cs2_exe, cs2_command_line(2560, 1440), steam_dir)
    assert args == [
        r"C:\Tools\HLAE\HLAE.exe",
        "-customLoader",
        "-noGui",
        "-autoStart",
        "-noConfig",
        "-hookDllPath", r"C:\Tools\HLAE\x64\AfxHookSource2.dll",
        "-programPath", str(cs2_exe),
        "-cmdLine", "-steam -insecure -sw -w 2560 -h 1440 -nojoy +exec cs2hl_boot",
        "-addEnv", r"SteamPath=C:\Program Files (x86)\Steam",
        "-addEnv", "SteamClientLaunch=1",
        "-addEnv", "SteamGameId=730",
        "-addEnv", "SteamAppId=730",
        "-addEnv", "SteamOverlayGameId=730",
    ]
    line = subprocess.list2cmdline(args)
    assert '-cmdLine "-steam -insecure -sw -w 2560 -h 1440 -nojoy +exec cs2hl_boot"' in line
    assert r'-addEnv "SteamPath=C:\Program Files (x86)\Steam"' in line


def test_steam_env_without_a_steam_folder():
    assert steam_env(None) == ["SteamClientLaunch=1", "SteamGameId=730", "SteamAppId=730", "SteamOverlayGameId=730"]


def test_hlae_process_env_drops_inherited_steam_ids_ignoring_case():
    inherited = {
        "PATH": r"C:\Windows",
        "STEAMAPPID": "2147483648",  # os.environ on Windows upper-cases names
        "SteamGameId": "15561574998464659456",
        "steamclientlaunch": "1",
        "SteamOverlayGameId": "15561574998464659456",
        "SteamPath": r"D:\OtherSteam",
        "SteamEnv": "1",
    }
    assert hlae_process_env(inherited) == {"PATH": r"C:\Windows", "SteamEnv": "1"}
    added = {item.split("=", 1)[0].lower() for item in steam_env(Path("Steam"))}
    assert added == {key.lower() for key in hlae.STEAM_ENV_KEYS}


def test_hlae_process_env_defaults_to_the_app_environment(monkeypatch):
    monkeypatch.setenv("SteamAppId", "2147483648")
    monkeypatch.setenv("CS2HL_TEST_VAR", "kept")
    env = hlae_process_env()
    assert "SteamAppId" not in env and env["CS2HL_TEST_VAR"] == "kept"


def test_config_derives_cs2_paths(tmp_path):
    config = HlaeConfig(cs2_dir=tmp_path / "CS2", hlae_exe=tmp_path / "HLAE.exe")
    assert config.cs2_exe == tmp_path / "CS2" / "game" / "bin" / "win64" / "cs2.exe"
    assert config.csgo_dir == tmp_path / "CS2" / "game" / "csgo"
    assert config.cfg_path == tmp_path / "CS2" / "game" / "csgo" / "cfg" / "cs2hl_boot.cfg"
    assert config.stage_dir == tmp_path / "CS2" / "game" / "csgo" / "cs2hl"
    assert config.launch and config.poll_interval_s == 1.0


def test_capture_settings_default_fits_common_monitors():
    assert CaptureSettings() == CaptureSettings(width=2560, height=1440, fps=60)


# ---------------------------------------------------------------- files


def test_stage_demo_copies_and_skips_identical_copy(tmp_path):
    src = tmp_path / "match.dem"
    src.write_bytes(b"abc")
    dest_dir = tmp_path / "cs2hl"
    dest_dir.mkdir()
    dest = dest_dir / "match.dem"
    dest.write_bytes(b"xyz")
    os.utime(dest, (src.stat().st_atime, src.stat().st_mtime))
    assert stage_demo(src, dest_dir) == dest
    assert dest.read_bytes() == b"xyz"
    os.utime(dest, (src.stat().st_atime, src.stat().st_mtime - 100))
    stage_demo(src, dest_dir)
    assert dest.read_bytes() == b"abc"


def test_take_files_prefers_newest_take_and_finds_its_wav(tmp_path):
    for take in ("take0000", "take0001"):
        (tmp_path / take).mkdir()
        (tmp_path / take / "video.mp4").write_bytes(b"x")
    (tmp_path / "take0001" / "audio.wav").write_bytes(b"w")
    (tmp_path / "take0002").mkdir()  # started but no video
    assert take_files(tmp_path) == (tmp_path / "take0001" / "video.mp4", tmp_path / "take0001" / "audio.wav")
    (tmp_path / "take0001" / "video.mp4").rename(tmp_path / "take0001" / "video.cs2hl.mp4")
    assert take_files(tmp_path)[0] == tmp_path / "take0001" / "video.cs2hl.mp4"
    assert take_files(tmp_path / "missing") == (None, None)


def test_is_settled_needs_same_nonzero_size_twice():
    seen: dict[str, int] = {}
    assert not is_settled(seen, "a", 0)
    assert not is_settled(seen, "a", 0)
    assert not is_settled(seen, "a", 10)
    assert not is_settled(seen, "a", 12)
    assert is_settled(seen, "a", 12)


def test_too_short_and_mux_command(tmp_path):
    assert too_short(1.0, 10.0) and not too_short(6.0, 10.0) and not too_short(1.0, 0.0)
    cmd = mux_command(tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "o.mp4")
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[-1] == str(tmp_path / "o.mp4")


# ---------------------------------------------------------------- backend with a fake CS2


class FakeHlaeProcess:
    def __init__(self, returncode, hangs):
        self.returncode = None if hangs else returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


class FakeWorld:
    """Stands in for Windows: running processes, HLAE, and a CS2 that writes the takes its boot cfg names."""

    def __init__(self, config: HlaeConfig):
        self.config = config
        self.running = {"steam.exe"}
        self.killed: list[str] = []
        self.launches: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.cfgs: list[str] = []
        self.procs: list[FakeHlaeProcess] = []
        self.skip: set[str] = set()
        self.media: dict[str, tuple[Path | None, Path | None]] = {}
        self.audio = False
        self.hlae_rc = 0
        self.hlae_hangs = False
        self.cs2_quits = True
        self.pause_before: str | None = None  # stop writing at this segment until killed
        self.seen_running = Event()
        self.stop = Event()
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def is_running(self, name: str) -> bool:
        with self.lock:
            running = name.lower() in self.running
        if running and name.lower() == "cs2.exe":
            self.seen_running.set()
        return running

    def kill(self, name: str, timeout_s: float = 10.0) -> int:
        with self.lock:
            was = name.lower() in self.running
            self.running.discard(name.lower())
        self.killed.append(name.lower())
        self.stop.set()
        self.seen_running.set()
        return int(was)

    def popen(self, args, env=None):
        self.launches.append(list(args))
        self.envs.append(None if env is None else dict(env))
        proc = FakeHlaeProcess(self.hlae_rc, self.hlae_hangs)
        self.procs.append(proc)
        if not self.hlae_hangs:
            self.start_cs2()
        return proc

    def start_cs2(self, delay_s: float = 0.0):
        self.seen_running.clear()
        self.stop.clear()
        if not delay_s:
            with self.lock:
                self.running.add("cs2.exe")
        thread = threading.Thread(target=self._cs2, args=(delay_s,), daemon=True)
        self.threads.append(thread)
        thread.start()

    def _cs2(self, delay_s: float):
        if delay_s:
            time.sleep(delay_s)
            with self.lock:
                self.running.add("cs2.exe")
        self.seen_running.wait(5)
        if self.stop.is_set():
            return
        text = self.config.cfg_path.read_text(encoding="utf-8")
        self.cfgs.append(text)
        for name in re.findall(r"record name \.\.\\\.\.\\csgo\\cs2hl\\work\\(\S+)", text):
            if name == self.pause_before:
                self.stop.wait(10)
            if self.stop.is_set():
                return
            if name in self.skip:
                continue
            take = self.config.stage_dir / "work" / name / "take0000"
            take.mkdir(parents=True)
            video, audio = self.media.get(name, (None, None))
            if video is not None:
                shutil.copy(video, take / "video.mp4")
            else:
                (take / "video.mp4").write_bytes(b"MP4:" + name.encode())
            if audio is not None:
                shutil.copy(audio, take / "audio.wav")
            elif self.audio:
                (take / "audio.wav").write_bytes(b"RIFF")
        if self.cs2_quits:
            with self.lock:
                self.running.discard("cs2.exe")

    def join(self):
        for thread in self.threads:
            thread.join(5)


@pytest.fixture
def world(tmp_path, monkeypatch):
    cs2_dir = tmp_path / "Counter-Strike Global Offensive"
    exe = cs2_dir / "game" / "bin" / "win64" / "cs2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (cs2_dir / "game" / "csgo").mkdir(parents=True)
    hlae_dir = tmp_path / "HLAE"
    (hlae_dir / "x64").mkdir(parents=True)
    (hlae_dir / "ffmpeg" / "bin").mkdir(parents=True)
    hlae_exe = hlae_dir / "HLAE.exe"
    for path in (hlae_exe, hlae_dir / "x64" / "AfxHookSource2.dll", hlae_dir / "ffmpeg" / "bin" / "ffmpeg.exe"):
        path.write_bytes(b"MZ")
    config = HlaeConfig(cs2_dir=cs2_dir, hlae_exe=hlae_exe, steam_dir=tmp_path / "Steam", session_timeout_s=10, poll_interval_s=0.01)
    fake = FakeWorld(config)
    monkeypatch.setattr(tools, "is_process_running", fake.is_running)
    monkeypatch.setattr(tools, "kill_process", fake.kill)
    monkeypatch.setattr(tools, "window_title_exists", lambda prefix: False)
    monkeypatch.setattr(hlae, "start_process", fake.popen)
    monkeypatch.setattr(ffmpeg_mod, "find_tool", lambda name: None)
    demo = tmp_path / "my match.dem"
    demo.write_bytes(b"DEMO")
    yield SimpleNamespace(config=config, fake=fake, demo=demo, out=tmp_path / "out", logs=[], tmp=tmp_path)
    fake.stop.set()
    fake.join()


def render(w, plan, config=None, cancel=None):
    backend = HlaeBackend(config or w.config)
    return backend.render(plan, w.demo, w.out, CaptureSettings(), w.logs.append, cancel or Event())


def test_render_records_every_segment_unattended(world):
    a, b = two_clip_plan_clips()
    results = render(world, make_plan(a, b))

    out = world.out
    assert results == [
        CaptureResult("clip001_r03", path=out / "clip001_r03_s0.mp4", parts=(out / "clip001_r03_s0.mp4", out / "clip001_r03_s1.mp4")),
        CaptureResult("clip002_r07", path=out / "clip002_r07_s0.mp4", parts=(out / "clip002_r07_s0.mp4",)),
    ]
    assert (out / "clip001_r03_s1.mp4").read_bytes() == b"MP4:clip001_r03_s1"
    assert (world.config.stage_dir / "my_match.dem").read_bytes() == b"DEMO"
    assert world.config.cfg_path.read_text(encoding="utf-8").endswith("playdemo cs2hl/my_match\n")
    assert len(world.fake.launches) == 1
    args = world.fake.launches[0]
    assert args[0] == str(world.config.hlae_exe)
    assert args[args.index("-cmdLine") + 1].endswith("-w 2560 -h 1440 -nojoy +exec cs2hl_boot")
    assert f"SteamPath={world.tmp / 'Steam'}" in args
    assert not any((world.config.stage_dir / "work").iterdir())  # takes are cleaned up
    assert "cs2.exe" not in world.fake.killed  # it quit on its own


def test_hlae_is_started_without_the_apps_steam_ids(world, monkeypatch):
    monkeypatch.setenv("SteamAppId", "2147483648")
    monkeypatch.setenv("SteamGameId", "15561574998464659456")
    render(world, make_plan(make_clip("a", 1000, 2000)))
    env = world.fake.envs[0]
    assert env is not None and "SteamAppId" not in env and "SteamGameId" not in env
    assert env.get("PATH") == os.environ.get("PATH")
    assert "SteamAppId=730" in world.fake.launches[0]


def test_already_running_cs2_is_refused(world):
    world.fake.running.add("cs2.exe")
    with pytest.raises(CaptureError, match="already running"):
        render(world, make_plan(make_clip("clip001_r03", 1000, 2000)))
    assert world.fake.launches == []
    assert not world.config.cfg_path.exists()


def test_steam_must_run_to_launch(world):
    world.fake.running.clear()
    with pytest.raises(CaptureError, match="Steam is not running"):
        render(world, make_plan(make_clip("clip001_r03", 1000, 2000)))


def test_missing_cs2_or_hlae_is_refused(world, tmp_path):
    with pytest.raises(CaptureError, match="CS2 folder not found"):
        render(world, make_plan(make_clip("a", 1000, 2000)), HlaeConfig(cs2_dir=tmp_path / "nope", hlae_exe=world.config.hlae_exe))
    with pytest.raises(CaptureError, match="HLAE was not found"):
        render(world, make_plan(make_clip("a", 1000, 2000)), HlaeConfig(cs2_dir=world.config.cs2_dir, hlae_exe=None))
    (world.config.hlae_exe.parent / "x64" / "AfxHookSource2.dll").unlink()
    with pytest.raises(CaptureError, match="HLAE not found"):
        render(world, make_plan(make_clip("a", 1000, 2000)))


def test_missing_segment_fails_its_clip_and_removes_its_other_parts(world):
    a, b = two_clip_plan_clips()
    world.fake.skip = {"clip001_r03_s1"}
    results = render(world, make_plan(a, b))
    assert results[0].path is None and "clip001_r03_s1: HLAE wrote no video" in results[0].error
    assert not (world.out / "clip001_r03_s0.mp4").exists()
    assert results[1].parts == (world.out / "clip002_r07_s0.mp4",)


def test_stale_outputs_of_a_clip_are_removed_before_recording(world):
    world.out.mkdir()
    (world.out / "clip001_r03_s5.mp4").write_bytes(b"old")
    render(world, make_plan(make_clip("clip001_r03", 1000, 2000)))
    assert sorted(p.name for p in world.out.iterdir()) == ["clip001_r03_s0.mp4"]


def test_cancel_kills_cs2_and_keeps_finished_segments(world):
    a, b = two_clip_plan_clips()
    world.fake.pause_before = "clip002_r07_s0"
    cancel = Event()

    def cancel_when_paused():
        deadline = time.monotonic() + 5
        while not (world.config.stage_dir / "work" / "clip001_r03_s1" / "take0000" / "video.mp4").exists():
            if time.monotonic() > deadline:
                return
            time.sleep(0.01)
        cancel.set()

    threading.Thread(target=cancel_when_paused, daemon=True).start()
    with pytest.raises(CaptureCancelled):
        render(world, make_plan(a, b), cancel=cancel)
    assert "cs2.exe" in world.fake.killed
    assert (world.out / "clip001_r03_s0.mp4").exists() and (world.out / "clip001_r03_s1.mp4").exists()
    assert not (world.out / "clip002_r07_s0.mp4").exists()


def test_hlae_failure_fails_every_clip_and_skips_later_runs(world):
    world.fake.hlae_rc = 1
    clips = [make_clip("a", 1000, 2000), make_clip("b", 1500, 2500, steamid=BOB)]  # two CS2 runs
    results = render(world, make_plan(*clips))
    assert len(world.fake.launches) == 1
    assert "exit code 1" in results[0].error
    assert results[1].error.startswith("not recorded: HLAE could not start CS2")
    assert "cs2.exe" in world.fake.killed  # Loader.cs resumes cs2.exe even after a failed injection


def test_hlae_that_never_exits_is_killed(world, monkeypatch):
    monkeypatch.setattr(hlae, "HLAE_START_TIMEOUT_S", 0.05)
    world.fake.hlae_hangs = True
    results = render(world, make_plan(make_clip("a", 1000, 2000)))
    assert world.fake.procs[0].killed
    assert "error" in results[0].error and "HLAE did not finish starting CS2" in results[0].error


def test_overlapping_clips_record_in_separate_runs(world):
    a = make_clip("a", 1000, 2000, slot=3)
    b = make_clip("b", 1500, 2500, slot=5, steamid=BOB)
    c = make_clip("c", 3000, 3500, slot=3)
    results = render(world, make_plan(a, b, c))
    assert len(world.fake.launches) == 2
    first, second = world.fake.cfgs
    assert "work\\a_s0" in first and "work\\c_s0" in first and "work\\b_s0" not in first
    assert "work\\b_s0" in second and "work\\a_s0" not in second
    assert [r.error for r in results] == [None, None, None]
    assert any("CS2 run 2 of 2" in line for line in world.logs)


def test_cs2_left_open_between_runs_stops_the_next_run(world, monkeypatch):
    monkeypatch.setattr(hlae, "CS2_START_TIMEOUT_S", 0.05)
    original = HlaeBackend._collect

    def collect_then_reopen(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        world.fake.running.add("cs2.exe")
        return out

    monkeypatch.setattr(HlaeBackend, "_collect", collect_then_reopen)
    results = render(world, make_plan(make_clip("a", 1000, 2000), make_clip("b", 1500, 2500, steamid=BOB)))
    assert results[0].error is None
    assert results[1].error == "CS2 from the previous run is still open"
    assert len(world.fake.launches) == 1


def test_debug_mode_waits_for_the_user_to_start_cs2(world):
    config = HlaeConfig(world.config.cs2_dir, world.config.hlae_exe, launch=False, session_timeout_s=10, poll_interval_s=0.01)
    world.fake.running.clear()  # no Steam check in debug mode
    world.fake.start_cs2(delay_s=0.1)
    results = render(world, make_plan(make_clip("clip001_r03", 1000, 2000)), config)
    assert world.fake.launches == []
    assert results[0].parts == (world.out / "clip001_r03_s0.mp4",)
    assert any(line.startswith("Debug mode: start CS2 yourself with: ") and "+exec cs2hl_boot" in line for line in world.logs)


def test_session_timeout_kills_cs2(world, monkeypatch):
    monkeypatch.setattr(hlae, "IDLE_AFTER_LAST_S", 60.0)
    config = HlaeConfig(world.config.cs2_dir, world.config.hlae_exe, session_timeout_s=0.2, poll_interval_s=0.01)
    world.fake.cs2_quits = False
    results = render(world, make_plan(make_clip("a", 1000, 2000), make_clip("b", 1500, 2500)), config)
    assert "cs2.exe" in world.fake.killed
    assert results[0].error is None  # its take was complete
    assert "CS2 did not finish" in results[1].error


def test_cs2_that_never_quits_is_closed_once_every_take_is_idle(world, monkeypatch):
    monkeypatch.setattr(hlae, "IDLE_AFTER_LAST_S", 0.1)
    world.fake.cs2_quits = False
    results = render(world, make_plan(*two_clip_plan_clips()))
    assert "cs2.exe" in world.fake.killed
    assert [r.error for r in results] == [None, None]
    assert any("did not quit" in line for line in world.logs)


def test_nothing_recorded_stops_with_a_clear_message(world, monkeypatch):
    monkeypatch.setattr(hlae, "FIRST_TAKE_TIMEOUT_S", 0.1)
    world.fake.cs2_quits = False
    world.fake.skip = {"a_s0"}
    results = render(world, make_plan(make_clip("a", 1000, 2000)))
    assert "nothing was recorded" in results[0].error
    assert "cs2.exe" in world.fake.killed


def test_stalled_session_is_closed_and_keeps_finished_clips(world, monkeypatch):
    monkeypatch.setattr(hlae, "STALL_AFTER_S", 0.2)
    world.fake.cs2_quits = False
    world.fake.skip = {"clip002_r07_s0"}  # CS2 hangs before this take starts
    a, b = two_clip_plan_clips()
    later = make_clip("c", 5500, 5800, slot=4)  # overlaps b, so it needs a second CS2 run
    results = render(world, make_plan(a, b, later))
    assert "cs2.exe" in world.fake.killed
    assert results[0].error is None
    assert "CS2 stopped recording: nothing new was written for 0.2 s" in results[1].error
    assert results[2].error.startswith("not recorded: CS2 stopped recording")
    assert len(world.fake.launches) == 1


def test_hook_error_window_stops_the_session(world, monkeypatch):
    monkeypatch.setattr(tools, "window_title_exists", lambda prefix: prefix == "Error - AfxHookSource")
    world.fake.cs2_quits = False
    world.fake.pause_before = "a_s0"
    results = render(world, make_plan(make_clip("a", 1000, 2000)))
    assert "Error - AfxHookSource2" in results[0].error


def test_hlae_without_ffmpeg_is_pointed_at_the_app_ffmpeg(world, monkeypatch):
    (world.config.hlae_exe.parent / "ffmpeg" / "bin" / "ffmpeg.exe").unlink()
    with pytest.raises(CaptureError, match="HLAE needs FFmpeg"):
        render(world, make_plan(make_clip("a", 1000, 2000)))
    app_ffmpeg = world.tmp / "app" / "ffmpeg.exe"
    app_ffmpeg.parent.mkdir()
    app_ffmpeg.write_bytes(b"MZ")
    monkeypatch.setattr(ffmpeg_mod, "find_tool", lambda name: str(app_ffmpeg) if name == "ffmpeg" else None)
    render(world, make_plan(make_clip("a", 1000, 2000)))
    assert tools.hlae_ffmpeg_path(world.config.hlae_exe) == app_ffmpeg


def test_unwritable_hlae_folder_is_a_clear_error(world, monkeypatch):
    (world.config.hlae_exe.parent / "ffmpeg" / "bin" / "ffmpeg.exe").unlink()
    monkeypatch.setattr(ffmpeg_mod, "find_tool", lambda name: "C:/app/ffmpeg.exe" if name == "ffmpeg" else None)

    def denied(hlae_exe, ffmpeg_exe):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(tools, "register_hlae_ffmpeg", denied)
    with pytest.raises(CaptureError, match="cannot write to its folder .*administrator"):
        render(world, make_plan(make_clip("a", 1000, 2000)))
    assert world.fake.launches == []


def test_wav_without_ffmpeg_keeps_a_silent_video(world):
    world.fake.audio = True
    results = render(world, make_plan(make_clip("a", 1000, 2000)))
    assert results[0].parts == (world.out / "a_s0.mp4",)
    assert any("has no sound" in line for line in world.logs)


def test_invalid_clip_gets_an_error_and_empty_plan_returns_nothing(world):
    results = render(world, make_plan(make_clip("a", 1000, 1000)))
    assert "empty segment" in results[0].error and world.fake.launches == []
    assert render(world, make_plan()) == []
    assert world.logs[-1] == "No clips to record."


def make_media(folder: Path, seconds: float) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    video, audio = folder / "video.mp4", folder / "audio.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate=60:duration={seconds}",
         "-pix_fmt", "yuv420p", str(video)],
        check=True, timeout=60,
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(audio)],
        check=True, timeout=60,
    )
    return video, audio


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
def test_wav_is_muxed_into_the_segment_file(world, monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_tool", shutil.which)
    video, audio = make_media(world.tmp / "media", 2)
    world.fake.media = {"a_s0": (video, audio)}
    results = render(world, make_plan(make_clip("a", 1000, 1128)))  # 128 ticks = 2 s
    assert results[0].error is None
    info = ffmpeg_mod.probe(world.out / "a_s0.mp4")
    assert info["has_audio"] and info["codec"] == "h264"
    assert abs(info["duration"] - 2) < 0.2


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
def test_broken_wav_falls_back_to_a_silent_video(world, monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_tool", shutil.which)
    video, _audio = make_media(world.tmp / "media", 2)
    bad_wav = world.tmp / "media" / "bad.wav"
    bad_wav.write_bytes(b"RIFF....not a wav")
    world.fake.media = {"a_s0": (video, bad_wav)}
    results = render(world, make_plan(make_clip("a", 1000, 1128)))
    assert results[0].error is None
    assert not ffmpeg_mod.probe(world.out / "a_s0.mp4")["has_audio"]
    assert any("Could not add sound to a_s0.mp4" in line for line in world.logs)
    assert sorted(p.name for p in world.out.iterdir()) == ["a_s0.mp4"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
def test_cut_off_or_broken_segment_is_rejected(world, monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_tool", shutil.which)
    video, _audio = make_media(world.tmp / "media", 1)
    world.fake.media = {"a_s0": (video, None)}
    results = render(world, make_plan(make_clip("a", 1000, 1640), make_clip("b", 3000, 3064)))  # a expects 10 s
    assert "stopped early: 1.0 s of 10.0 s" in results[0].error
    assert "unreadable recording" in results[1].error  # b's take is not a real mp4
    assert list(world.out.iterdir()) == []


def test_session_watch_reasons_with_a_fake_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "window_title_exists", lambda prefix: False)
    now = [0.0]
    dirs = [tmp_path / "a_s0", tmp_path / "a_s1"]
    for d in dirs:
        d.mkdir()
    watch = hlae._SessionWatch(dirs, clock=lambda: now[0], first_take_s=100, idle_s=30)
    assert watch.check() is None
    now[0] = 101
    assert watch.check() == "no-start"

    (dirs[0] / "take0000").mkdir()
    (dirs[0] / "take0000" / "video.mp4").write_bytes(b"12")
    assert watch.check() is None  # second take not there yet
    (dirs[1] / "take0000").mkdir()
    (dirs[1] / "take0000" / "video.mp4").write_bytes(b"1")
    assert watch.check() is None
    now[0] = 120
    (dirs[1] / "take0000" / "video.mp4").write_bytes(b"123")  # still growing
    assert watch.check() is None
    now[0] = 149
    assert watch.check() is None
    now[0] = 150
    assert watch() and watch.reason == "finished"

    monkeypatch.setattr(tools, "window_title_exists", lambda prefix: True)
    assert watch.check() == "hook-error"


def test_session_watch_stops_when_a_later_take_never_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "window_title_exists", lambda prefix: False)
    now = [0.0]
    dirs = [tmp_path / f"a_s{i}" for i in range(3)]
    for d in dirs:
        d.mkdir()
    watch = hlae._SessionWatch(dirs, clock=lambda: now[0], first_take_s=300, idle_s=60, stall_s=240)
    video = dirs[0] / "take0000" / "video.mp4"
    video.parent.mkdir()
    video.write_bytes(b"1")
    now[0] = 10
    assert watch.check() is None
    now[0] = 200  # a long seek or a slow segment: still fine
    video.write_bytes(b"12")
    assert watch.check() is None
    (dirs[1] / "take0000").mkdir()  # the next take appears; its video comes a little later
    now[0] = 430
    assert watch.check() is None  # the new folder counts as progress
    now[0] = 669
    assert watch.check() is None
    now[0] = 670
    assert watch() and watch.reason == "stalled"
    assert "240 s" in watch.message() and "closed" in watch.message()


def test_session_watch_prefers_finished_over_stalled(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "window_title_exists", lambda prefix: False)
    now = [0.0]
    take = tmp_path / "a_s0" / "take0000"
    take.mkdir(parents=True)
    (take / "video.mp4").write_bytes(b"1")
    watch = hlae._SessionWatch([tmp_path / "a_s0"], clock=lambda: now[0], idle_s=60, stall_s=30)
    assert watch.check() is None
    now[0] = 60
    assert watch.check() == "finished"
