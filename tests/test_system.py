import sys
from pathlib import Path
from threading import Event

import pytest

from cs2hl.system import preflight as preflight_mod
from cs2hl.system import steam, tools
from cs2hl.system.preflight import Check, all_ok, preflight, problems
from cs2hl.system.steam import (
    cs2_installdir,
    find_cs2_dir,
    libraries,
    library_paths,
    parse_vdf,
    read_registry,
    steam_root,
    steam_root_candidates,
)

LIBRARYFOLDERS = r'''"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"contentid"		"1234567890123456789"
		"totalsize"		"0"
		"update_clean_bytes_tally"		"80234567"
		"time_last_update_verified"		"1730000000"
		"apps"
		{
			"228980"		"1048576"
			"250820"		"5120000000"
		}
	}
	"1"
	{
		"path"		"D:\\SteamLibrary"
		"label"		"Games \"fast\""
		"contentid"		"987654321"
		"totalsize"		"2000396742656"
		"apps"
		{
			"730"		"64012345678"
		}
	}
}
'''

APPMANIFEST = '''"AppState"
{
	"appid"		"730"
	"universe"		"1"
	"LauncherPath"		"C:\\\\Program Files (x86)\\\\Steam\\\\steam.exe"
	"name"		"Counter-Strike 2"
	"StateFlags"		"4"
	"installdir"		"Counter-Strike Global Offensive"
	"InstalledDepots"
	{
		"2347770"
		{
			"manifest"		"1111111111111111111"
			"size"		"41000000000"
		}
	}
}
'''


def vdf_text(entries: dict[str, tuple[Path, list[str]]]) -> str:
    """libraryfolders.vdf in Steam's current format for real paths (backslashes escaped)."""
    lines = ['"libraryfolders"', "{"]
    for number, (path, apps) in entries.items():
        escaped = str(path).replace("\\", "\\\\")
        lines += [f'\t"{number}"', "\t{", f'\t\t"path"\t\t"{escaped}"', '\t\t"apps"', "\t\t{"]
        lines += [f'\t\t\t"{app}"\t\t"1000"' for app in apps]
        lines += ["\t\t}", "\t}"]
    lines.append("}")
    return "\n".join(lines) + "\n"


def make_cs2(library: Path, installdir: str = "Counter-Strike Global Offensive", manifest: bool = True) -> Path:
    game = library / "steamapps" / "common" / installdir
    exe = game / "game" / "bin" / "win64" / "cs2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    if manifest:
        acf = library / "steamapps" / "appmanifest_730.acf"
        acf.write_text(APPMANIFEST.replace("Counter-Strike Global Offensive", installdir), encoding="utf-8")
    return game


def make_hlae(folder: Path, with_dll: bool = True, with_injector: bool = True) -> Path:
    exe = folder / "HLAE.exe"
    (folder / "x64").mkdir(parents=True)
    exe.write_bytes(b"MZ")
    if with_dll:
        (folder / "x64" / "AfxHookSource2.dll").write_bytes(b"MZ")
    if with_injector:
        (folder / "x64" / "injector.exe").write_bytes(b"MZ")
    return exe


def test_parse_vdf_reads_current_libraryfolders_format():
    data = parse_vdf(LIBRARYFOLDERS)
    folders = data["libraryfolders"]
    assert folders["0"]["path"] == r"C:\Program Files (x86)\Steam"
    assert folders["1"]["path"] == r"D:\SteamLibrary"
    assert folders["1"]["label"] == 'Games "fast"'
    assert set(folders["0"]["apps"]) == {"228980", "250820"}
    assert folders["1"]["apps"] == {"730": "64012345678"}


def test_parse_vdf_reads_appmanifest_nested_blocks():
    state = parse_vdf(APPMANIFEST)["AppState"]
    assert state["installdir"] == "Counter-Strike Global Offensive"
    assert state["LauncherPath"] == r"C:\Program Files (x86)\Steam\steam.exe"
    assert state["InstalledDepots"]["2347770"]["size"] == "41000000000"


def test_parse_vdf_handles_old_format_comments_bom_bare_tokens_and_conditionals():
    text = (
        "\ufeff// Steam library list\n"
        '"LibraryFolders"\n{\n'
        '\t"TimeNextStatsReport"\t\t"1700000000"\n'
        '\t"1"\t\t"E:\\\\Games\\\\Steam"  // second drive\n'
        "\tbare_key bare_value [$WIN32]\n"
        "}\n"
    )
    folders = parse_vdf(text)["LibraryFolders"]
    assert folders == {"TimeNextStatsReport": "1700000000", "1": r"E:\Games\Steam", "bare_key": "bare_value"}


@pytest.mark.parametrize("text", ['"a" { "b" "c"', '"a" "b" }', '{ "a" "b" }', '"a" "unterminated', '"a"'])
def test_parse_vdf_rejects_broken_files(text):
    with pytest.raises(ValueError):
        parse_vdf(text)


def test_steam_root_candidates_follow_registry_order_and_dedupe():
    values = {
        ("HKEY_CURRENT_USER", "SteamPath"): "c:/program files (x86)/steam",
        ("HKEY_LOCAL_MACHINE", "InstallPath"): r"D:\Steam",
    }
    seen: list[tuple[str, str, str]] = []

    def read(hive, key, value):
        seen.append((hive, key, value))
        return values.get((hive, value))

    candidates = steam_root_candidates(read, fallback=r"D:\Steam")
    assert candidates == [Path("c:/program files (x86)/steam"), Path(r"D:\Steam")]
    assert seen == [
        ("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ]


def test_steam_root_falls_back_past_missing_folders(tmp_path):
    install = tmp_path / "InstallPathSteam"
    default = tmp_path / "DefaultSteam"
    default.mkdir()

    def read(hive, key, value):
        return {"SteamPath": str(tmp_path / "gone"), "InstallPath": str(install)}.get(value)

    assert steam_root(read, fallback=str(default)) == default
    install.mkdir()
    assert steam_root(read, fallback=str(default)) == install
    assert steam_root(lambda *a: None, fallback=str(tmp_path / "nope")) is None


def test_read_registry_is_none_without_winreg():
    if sys.platform == "win32":
        pytest.skip("winreg exists on Windows")
    assert read_registry("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath") is None


def test_libraries_put_steam_first_and_merge_its_listed_apps(tmp_path):
    root = tmp_path / "Steam"
    lib = tmp_path / "Lib2"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        vdf_text({"0": (root, ["228980"]), "1": (lib, ["730"])}), encoding="utf-8"
    )
    assert libraries(root) == [(root, frozenset({"228980"})), (lib, frozenset({"730"}))]
    assert library_paths(root) == [root, lib]


def test_libraries_without_vdf_is_just_steam(tmp_path):
    assert library_paths(tmp_path) == [tmp_path]


def test_find_cs2_dir_uses_library_listing_730_and_manifest_installdir(tmp_path):
    root = tmp_path / "Steam"
    lib = tmp_path / "Lib2"
    (root / "steamapps").mkdir(parents=True)
    (lib / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        vdf_text({"0": (root, ["228980"]), "1": (lib, ["730"])}), encoding="utf-8"
    )
    game = make_cs2(lib, installdir="CS2 Custom")
    assert cs2_installdir(lib) == "CS2 Custom"
    assert find_cs2_dir(root) == game


def test_find_cs2_dir_survives_a_stale_listing_and_missing_manifest(tmp_path):
    root = tmp_path / "Steam"
    listed = tmp_path / "Listed"
    real = tmp_path / "Real"
    (root / "steamapps").mkdir(parents=True)
    (listed / "steamapps").mkdir(parents=True)
    (real / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        vdf_text({"0": (root, []), "1": (listed, ["730"]), "2": (real, [])}), encoding="utf-8"
    )
    game = make_cs2(real, manifest=False)
    assert find_cs2_dir(root) == game


def test_find_cs2_dir_needs_cs2_exe(tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game").mkdir(parents=True)
    assert find_cs2_dir(root) is None


def test_find_cs2_dir_without_steam_is_none(monkeypatch):
    monkeypatch.setattr(steam, "steam_root", lambda *a, **k: None)
    assert find_cs2_dir() is None


def test_find_hlae_from_hint_exe_or_folder(tmp_path):
    exe = make_hlae(tmp_path / "HLAE")
    assert tools.find_hlae(exe, env={}) == exe
    assert tools.find_hlae(tmp_path / "HLAE", env={}) == exe
    assert tools.find_hlae(str(tmp_path / "HLAE"), env={}) == exe


def test_find_hlae_searches_common_folders(tmp_path):
    exe = make_hlae(tmp_path / "home" / ".csdm" / "hlae")
    env = {"USERPROFILE": str(tmp_path / "home"), "ProgramFiles(x86)": str(tmp_path / "pf86")}
    assert tools.find_hlae(None, env=env) == exe
    assert tools.find_hlae(tmp_path / "wrong", env=env) == exe


def test_find_hlae_needs_the_hook_dll(tmp_path):
    make_hlae(tmp_path / "HLAE", with_dll=False)
    assert tools.find_hlae(tmp_path / "HLAE", env={}) is None
    assert tools.find_hlae(None, env={}) is None


def test_hlae_search_dirs_order(tmp_path):
    env = {
        "LOCALAPPDATA": "/la",
        "ProgramFiles(x86)": "/pf86",
        "ProgramFiles": "/pf",
        "USERPROFILE": "/home",
        "SystemDrive": "/drive",
    }
    dirs = tools.hlae_search_dirs(env, app_dir=Path("/app"))
    assert dirs[:4] == [Path("/app/HLAE"), Path("/la/cs2hl/HLAE"), Path("/pf86/HLAE"), Path("/pf/HLAE")]
    assert Path("/home/.csdm/hlae") in dirs
    assert dirs[-1] == Path("/drive/HLAE")


def test_hlae_ffmpeg_from_ini_then_bin(tmp_path):
    exe = make_hlae(tmp_path / "HLAE")
    assert tools.hlae_ffmpeg_path(exe) is None
    assert not tools.hlae_has_ffmpeg(exe)

    bundled = tmp_path / "HLAE" / "ffmpeg" / "bin" / "ffmpeg.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"MZ")
    assert tools.hlae_ffmpeg_path(exe) == bundled

    custom = tmp_path / "Tools" / "ffmpeg.exe"
    custom.parent.mkdir()
    custom.write_bytes(b"MZ")
    ini = tmp_path / "HLAE" / "ffmpeg" / "ffmpeg.ini"
    ini.write_bytes(f'[FFMPEG]\r\npath="{custom}"\r\n'.encode("cp1252"))
    assert tools.hlae_ffmpeg_path(exe) == custom

    ini.write_text("[Ffmpeg]\nPath=" + str(tmp_path / "missing.exe") + "\n", encoding="utf-8")
    assert tools.hlae_ffmpeg_path(exe) is None  # HLAE uses the configured path even when it is wrong


def test_register_hlae_ffmpeg_is_read_back(tmp_path):
    exe = make_hlae(tmp_path / "HLAE")
    app_ffmpeg = tmp_path / "app" / "ffmpeg.exe"
    app_ffmpeg.parent.mkdir()
    app_ffmpeg.write_bytes(b"MZ")
    ini = tools.register_hlae_ffmpeg(exe, app_ffmpeg)
    assert ini == tmp_path / "HLAE" / "ffmpeg" / "ffmpeg.ini"
    assert ini.read_text(encoding="utf-8") == f"[Ffmpeg]\nPath={app_ffmpeg}\n"
    assert tools.hlae_ffmpeg_path(exe) == app_ffmpeg


class FakeProc:
    def __init__(self, name):
        self.info = {"name": name}
        self.killed = False

    def kill(self):
        self.killed = True


def test_process_helpers_match_names_case_insensitively(monkeypatch):
    procs = [FakeProc("System"), FakeProc("CS2.EXE"), FakeProc(None), FakeProc("cs2.exe")]
    waited = []
    monkeypatch.setattr(tools.psutil, "process_iter", lambda attrs: iter(procs))
    monkeypatch.setattr(tools.psutil, "wait_procs", lambda ps, timeout: waited.append((list(ps), timeout)))

    assert tools.is_process_running("cs2.exe")
    assert not tools.is_process_running("steam.exe")
    assert tools.kill_process("cs2.exe", timeout_s=3) == 2
    assert [p.killed for p in procs] == [False, True, False, True]
    assert waited == [([procs[1], procs[3]], 3)]
    assert tools.kill_process("steam.exe") == 0


def test_real_process_lookup_does_not_crash():
    assert tools.is_process_running("cs2hl-no-such-process.exe") is False


def test_wait_until_outcomes():
    cancel = Event()
    assert tools.wait_until(lambda: True, cancel, 1, 0.001) == "done"
    assert tools.wait_until(lambda: False, cancel, 0.02, 0.001) == "timeout"
    assert tools.wait_until(lambda: False, cancel, 5, 0.001, abort=lambda: True) == "aborted"
    cancel.set()
    assert tools.wait_until(lambda: False, cancel, 5, 0.001) == "cancelled"


def test_wait_for_start_and_exit_follow_the_process(monkeypatch):
    states = iter([False, False, True, True, True, False])
    monkeypatch.setattr(tools, "is_process_running", lambda name: next(states))
    assert tools.wait_for_start("cs2.exe", Event(), 5, 0.001) == "done"
    assert tools.wait_for_exit("cs2.exe", Event(), 5, 0.001) == "done"


def test_window_title_check_is_false_off_windows():
    if sys.platform == "win32":
        pytest.skip("real check on Windows")
    assert tools.window_title_exists(tools.HOOK_ERROR_TITLE) is False


def test_preflight_runs_on_linux_and_reports_not_ok():
    checks = preflight(None, None)
    assert [c.name for c in checks] == [
        "Windows",
        "Steam running",
        "CS2 installed",
        "CS2 closed",
        "HLAE installed",
        "HLAE FFmpeg",
        "FFmpeg",
    ]
    assert all(isinstance(c, Check) and c.detail for c in checks)
    by_name = {c.name: c for c in checks}
    assert not by_name["CS2 installed"].ok
    assert not by_name["HLAE installed"].ok
    assert not by_name["HLAE FFmpeg"].ok
    if sys.platform != "win32":
        assert not by_name["Windows"].ok
        assert not all_ok(checks)


def test_preflight_all_ok_with_fakes(tmp_path, monkeypatch):
    game = make_cs2(tmp_path / "lib")
    hlae_exe = make_hlae(tmp_path / "HLAE")
    monkeypatch.setattr(preflight_mod, "is_windows", lambda: True)
    monkeypatch.setattr(tools, "is_process_running", lambda name: name == "steam.exe")
    monkeypatch.setattr(preflight_mod.ffmpeg, "find_tool", lambda name: "C:/app/ffmpeg.exe")

    checks = preflight(game, hlae_exe)
    assert problems(checks) == []
    assert all_ok(checks)
    hlae_ffmpeg = next(c for c in checks if c.name == "HLAE FFmpeg")
    assert "point it at C:/app/ffmpeg.exe" in hlae_ffmpeg.detail


def test_preflight_flags_running_cs2_missing_hook_and_ffmpeg(tmp_path, monkeypatch):
    game = make_cs2(tmp_path / "lib")
    hlae_exe = make_hlae(tmp_path / "HLAE", with_injector=False)
    monkeypatch.setattr(tools, "is_process_running", lambda name: name in ("steam.exe", "cs2.exe"))
    monkeypatch.setattr(preflight_mod.ffmpeg, "find_tool", lambda name: None)

    bad = {c.name: c for c in problems(preflight(game, hlae_exe))}
    assert "CS2 closed" in bad and "Close CS2" in bad["CS2 closed"].detail
    assert "injector.exe" in bad["HLAE installed"].detail
    assert "HLAE FFmpeg" in bad and "FFmpeg" in bad
    assert "CS2 installed" not in bad and "Steam running" not in bad


def test_preflight_cs2_folder_without_exe(tmp_path):
    check = next(c for c in preflight(tmp_path, None) if c.name == "CS2 installed")
    assert not check.ok and "cs2.exe" in check.detail
