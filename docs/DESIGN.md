# Design notes

Research date: 2026-10-10. Searches were quick and not exhaustive. Each source is
listed so you can check it yourself.

## 1. Similar projects

| Project | What it does | Relevance |
|---|---|---|
| [CS2-insight-agent](https://www.sourcepulse.org/projects/29723172) | Desktop app (React front end, FastAPI back end) that uses demoparser2 to find highlights, fails and "meme" deaths, then records through OBS (obs-websocket) and CS2 Game State Integration | Closest match to what we're building. Recording depends on OBS |
| [CS:DM / cs-demo-manager](https://cs-demo-manager.com/docs/guides/video) | Desktop app that launches CS, plays demos, records through HLAE or `startmovie`, then encodes with FFmpeg | Shows the proven capture-and-encode pipeline. Its recording is Windows-only for HLAE |
| [HLAE](https://wiki.sourceruns.org/wiki/HLAE) (advancedfx) | Recording hook with `mirv_streams` | The CS2 hook (AfxHookSource2) is small, and the maintainers say they are unsure they will keep developing it |
| [awpy](https://awpy.readthedocs.io/en/stable/other-parsers.html) | Python library for demo parsing and analysis, v2.0.2 on PyPI | Alternative to a raw parser. Heavier than we need for now |
| [demoparser2](https://github.com/RPSam/demoparser2) | Rust parser with Python and JS bindings, returns DataFrames | **What we use** |
| [demoinfocs-golang](https://github.com/markus-wa/demoinfocs-golang/) | Go parser, event-driven API, CS2 support | Good if we ever move the core to Go |
| [@akiver/cs-demo-analyzer](https://classic.yarnpkg.com/en/package/@akiver/cs-demo-analyzer) | MIT-licensed TypeScript analyzer | Good for JS/Electron; we chose Python |
| [Fragfinder-GUI](https://github.com/Arad119/Fragfinder-GUI) | Highlight timestamps from JSON exports | Its author has stopped work on it for CS2. Old |

What I did not find in these searches: a single local tool that analyzes the demo,
records the clips from the game, and reframes them to 9:16 automatically. The
closest ones need OBS or a Windows-only recorder. This is a quick search, not a
full survey.

## 2. Pipeline

```
.dem ──► analyze ──► plan.json ──► capture (CS2 on your PC) ──► <clip>.mp4 ──► export ──► 16:9 + 9:16 files
        demoparser2   highlights      startmovie / HLAE / OBS       raw         ffmpeg
        (no game)     + clip windows  (needs the game)
```

The split matters. Analysis doesn't need CS2 or a GPU, so it can be tested in CI.
The plan file is the contract between the stages.

## 3. Decisions made

1. **Python 3.11+ core.** demoparser2 and awpy both ship Python bindings. FFmpeg is
   driven as a CLI, so Python is only glue.
2. **Highlight types for v1:** multikill (2K+, 10 s window), clutch (1vN won), team
   ace. Scores add bonuses for headshot, noscope, smoke, wallbang, knife, and a win
   bonus. Weights are first guesses in `cs2hl/scoring.py`.
3. **Clip window:** 4 s before the first kill, 2 s after the last. Windows from the
   same player and round are merged. Clamped to the round start.
4. **9:16 has two modes.** `crop` zooms into the middle of the 16:9 frame. It is the
   sharpest option when recorded at 2160p. Its drawback is that the kill feed on the
   right can be cut, so `--x-offset` moves the window. `fit` keeps the whole frame
   over a blurred copy of itself and loses nothing.
5. **Encode:** H.264 High, yuv420p, CRF 17, preset slow, `+faststart`, AAC 192k.
   Output size is set by `--short-side` (1080, 1440 or 2160).
6. **Tests:** pure logic is tested directly. The demoparser2 adapter is tested with
   a fake parser, which checks our logic but not the real column names.

## 4. Open questions

These decide the next steps. Please answer what you can.

1. **Which OS is the PC you play CS2 on?** HLAE only works on Windows.
2. **Capture backend.** Each option has a cost:
   - **HLAE `mirv_streams` (Windows only):** pipes frames straight into FFmpeg, so
     there are no raw files, and the quality is best. CS2 support is limited, and
     its maintainers are unsure of continued work. *My first choice on Windows.*
   - **CS2 built-in `startmovie`:** the native command, so it should not depend on
     HLAE or the OS (not verified on CS2 yet). It writes uncompressed TGA frames
     plus a WAV file, which CS:DM's docs describe. At 1080p that is roughly 6 to 8 MB
     per frame, so a 20-second clip at 60 fps is about 7 to 10 GB before encoding.
     *My fallback.*
   - **OBS over websocket (what CS2-insight-agent does):** real-time, compressed,
     works anywhere. Timing is wall-clock, so cuts are less exact.
3. **Highlight taste.** Which matter most to you? Do you want only your own POV, or
   also other players' best moments? What clip length do you want (e.g. 15 to 30 s)?
4. **UI.** The CLI is enough to start. Later I'd suggest a local web UI that runs on
   `localhost` and opens in the browser. That is easy to package and avoids Electron.
   Tell me if you'd rather have a native desktop window.
5. **A real demo file.** The parser is the largest unverified part. A real `.dem`
   would let me check the column names and run the whole pipeline. Say how you'd
   like to share it. Don't paste it in chat, since it's binary.

## 5. Roadmap

1. Run the parser on a real demo and fix column mismatches (needs open question 5).
2. Build the capture backend chosen in question 2, producing `<clip id>.mp4`.
3. Tune scores on real matches. Review false positives, and add highlight types
   (e.g. long-range sniper kills, spray control) only if they show up in practice.
4. Local UI: list highlights, preview, toggle clips, export.
5. Package for Windows (PyInstaller or similar), so it runs without installing Python.

## 6. HLAE check against its source (2026-10-10)

The first research pass read HLAE's documentation, which does not list `mirv_streams`
for CS2. HLAE's source does. The clone used is `advancedfx/advancedfx` at commit
`ebbcd88`, read-only. The findings below are from code, not from docs.

Verified in the CS2 build (`AfxHookSource2/` and `shared/`, which the CS2 CMake compiles):

| Need | Command | Where |
|---|---|---|
| Recording to ffmpeg | `mirv_streams record screen enabled 1`, `mirv_streams record screen settings <name>` | `RenderSystemDX11Hooks.cpp` (`Console_RecordScreen`) |
| Custom ffmpeg preset | `mirv_streams settings add ffmpeg <name> "<options>"` (name must not start with `afx`) | `shared/RecordingSettings.cpp` |
| Output folder | `mirv_streams record name <path>`, then `<path>\take0000\` per start | `RenderSystemDX11Hooks.cpp` (`RecordStart`) |
| Frame rate | `mirv_streams record fps <value>` | `RenderSystemDX11Hooks.cpp` |
| Timed commands | `mirv_cmd addAtTick <tick> <command...>`, keyed on the **demo** tick | `shared/CommandSystem.cpp`, `AfxHookSource2/main.cpp` |
| Seek | `demo_gototick <tick>` (HLAE's `mirv_skip` sends it) | `AfxHookSource2/main.cpp` |
| ffmpeg binary | `<HLAE>\ffmpeg\ffmpeg.ini` [Ffmpeg] path, else `<HLAE>\ffmpeg\bin\ffmpeg.exe` | `shared/AfxOutStreams.cpp` |

Not in the CS2 build:

- `mirv_pov` (POV for GOTV demos) and any CS2 spectator setter. The only POV code
  reads the observer target. `spec_player` exists only in the CS:GO tree. So the app
  cannot yet choose a player's view in a CS2 demo. The recording shows the demo's camera.

Consequences for the design:

1. Clips that overlap in demo time cannot share one CS2 session. The HLAE backend runs
   one session per group of non-overlapping clips, so overlapping highlights take
   extra CS2 runs.
2. `mirv_cmd addAtTick` joins its parts with single spaces and drops quotes, so the
   record folder is a relative path with no spaces, resolved from `game\bin\win64`.
   The working directory is supported only by a changelog note, not by running CS2.
3. A preset name cannot be redefined in one CS2 run. The app always uses `cs2hl_h264`.

## 7. Still unverified (needs a real CS2 run and a real demo)

- Column names in demoparser2 output. The raw CS2 event fields are `attacker` and
  `userid` (controller entities). The adapter in `cs2hl/parse.py` assumes
  `attacker_steamid`, `user_steamid`, `attacker_name`, and team numbers 2/3 for the
  round winner. Those need checking on the first real demo.
- Whether `exec cs2hl/session` finds `game\csgo\cs2hl\session.cfg`, and whether
  `playdemo cs2hl/<name>` resolves the copied demo.
- Whether `quit` at a tick ends CS2, and whether `record end` at a tick runs before
  the seek to the next clip.
- POV: whether `spec_player <slot>` switches the view in CS2 demo playback, and how
  to map a SteamID to a slot.
- Whether HLAE's custom loader starts with `-customLoader -programPath ... -cmdLine
  "-steam -insecure +exec ..."` as the `hlae_launch_command` helper assumes.
- Windows packaging: PyInstaller output on Windows, and the ffmpeg download in CI.

## 8. Next steps

1. Run `cs2hl-gui` on a real demo on the Windows PC. Check the rounds and players
   tables against what you see in the match.
2. Record one short clip with the HLAE backend. Confirm the generated `session.cfg`
   runs, the take folder appears, and the clip reaches `recordings/`.
3. Decide POV: find the CS2 command that switches the view, or accept the demo camera
   for now.

## 9. Version 0.2: unattended recording (2026-10-15)

Sections 6-8 describe the first HLAE design, where the user typed `exec` in the console.
Version 0.2 replaces it. The sources for this section are HLAE (`advancedfx/advancedfx`,
MIT) and CS Demo Manager (`akiver/cs-demo-manager`, MIT), both read locally, plus runs of
the parser on two public demos.

What changed and why:

| Area | 0.1 | 0.2 | Evidence |
|---|---|---|---|
| Round winner | assumed 2/3 | `T`/`CT` strings (2/3 still accepted) | real demos crashed 0.1 |
| Rounds | every round_start | rounds after the last `begin_new_match`; knife and warmup dropped | FACEIT demo: 25 -> 24 rounds |
| Tickrate | header keys (absent) | from `game_time`, fallback 64 | both demos 64.0 |
| Self-check | none | compares rounds, kills, deaths and score with the game's counters | both demos match exactly |
| Highlights | streaks with a 10 s gap, team ace | one per player per round, kill windows cut together | FACEIT: 30 clips / 971 s / 22 team aces -> 42 clips / 407 s recorded |
| Camera | none | `spec_mode 1` + `spec_player <user_id + 1>` per piece | CS Demo Manager convention; slot equals `m_hController & 0x3FFF` on both demos |
| Kill feed | full HUD | `cl_draw_only_deathnotices 1` + `mirv_deathmsg` filter for the player | CS Demo Manager recipe |
| Launch | user types `exec` | `HLAE.exe -customLoader -noGui -autoStart` with `+exec cs2hl_boot` | HLAE `Program.cs`, CS Demo Manager launcher |
| Done | poll files until timeout | `quit` in the script, wait for `cs2.exe` to exit | CS Demo Manager |
| Output | one file per clip | pieces joined per clip; clips, montage or both | export tests with sound-sync markers |
| Paths | typed by the user | Steam registry key + `libraryfolders.vdf` | CS Demo Manager |

One HLAE detail matters for the schedule: `mirv_cmd`'s interval tree only follows one
branch on a big forward step, so commands added out of tick order can be skipped. The
generated script adds commands in tick order (simulated in Python from `CommandSystem.h`).

Still unverified until the first run on Windows:

- `+exec cs2hl_boot` runs after HLAE registers its `mirv_` commands, and `playdemo` from that
  script loads the demo.
- `spec_player <slot>` follows the chosen player in GOTV demos.
- The HUD and kill-feed commands work in demo playback without `sv_cheats`.
- A `-sw -w 2560 -h 1440` window records at full size on the user's monitor.
- HLAE's FFmpeg closes each `video.mp4` before the app collects it.

First run checklist (Setup tab all green, then one short demo and one player):

1. The CS2 console shows `Recording to "...\work\<clip>_s0\take0000"` for each piece and no
   `Unknown command` lines.
2. Each clip shows the chosen player's view, and the kill feed shows only their kills.
3. CS2 closes by itself at the end and the videos appear in the output folder.
4. If something fails, the app's log and `<CS2>\game\csgo\cfg\cs2hl_boot.cfg` are what to send.
