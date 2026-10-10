# cs2hl: local CS2 highlight generator

Pick a CS2 demo, choose the player and rounds, and get highlight clips in 16:9 and 9:16.
Everything runs on your PC. The desktop app records clips through HLAE and CS2.

**Status: early.** Analysis, the desktop UI, the export step and the HLAE command
generator are built and tested. Parsing a real `.dem` file and recording from CS2 have
not been run yet. See [Status](#status).

## Desktop app

```bash
python -m venv .venv
.venv/bin/pip install -e ".[gui,dev]"          # Windows: .venv\Scripts\pip install -e ".[gui,dev]"
.venv/bin/cs2hl-gui                             # or: .venv/bin/python -m cs2hl.gui
```

1. **Open demo.** The app scans it in the background: map, duration, rounds and players.
2. **Pick rounds** (all are included by default) and **tick the player(s)** to make highlights for.
3. **Preview highlights** lists the clips that would be made.
4. **Render** records each clip and exports the aspects you chose. The Backend setting
   chooses "Plan only" (write the plan JSON and stop) or HLAE (record in CS2).

Settings are remembered between runs.

## Command line

```bash
cs2hl analyze match.dem --players 76561198000000000 --rounds 1 2 3 --max-clips 10
cs2hl export match.plan.json --recordings recordings/ --aspect 16x9 9x16 --vertical-mode crop
```

`--players` takes SteamID64 values. `cs2hl analyze` without `--players` includes everyone.

## Requirements

- Windows for recording. HLAE only runs on Windows.
- Counter-Strike 2 and [HLAE](https://github.com/advancedfx/advancedfx/releases) installed separately.
- FFmpeg. The packaged app expects `ffmpeg.exe` next to it. HLAE needs its own ffmpeg:
  `<HLAE>\ffmpeg\bin\ffmpeg.exe`, or a path in `<HLAE>\ffmpeg\ffmpeg.ini`.

## Highlights (v1)

| Kind | Meaning | Example label |
|---|---|---|
| multikill | 2+ kills by one player, each within 10 s of the last | `3K`, `ACE` |
| clutch | last player alive vs N enemies, and the team wins the round | `1v2` |
| team ace | one side kills every enemy alive at round start | `TEAM ACE` |

Bonuses for headshots, noscopes, through smoke, wallbangs and knife kills, and a win
bonus. The weights are in `cs2hl/scoring.py` and are first guesses.

## Layout

```
cs2hl/
  parse.py          demoparser2 -> DemoData                      [not yet run on a real demo]
  scan.py           players and rounds summary for the UI         [tested]
  detect.py         multikills, clutches, team aces               [tested]
  selection.py      keep the chosen players and rounds            [tested]
  pipeline.py       plan_highlights, render_plan                  [tested]
  planner.py        clip windows, merging, plan JSON              [tested]
  capture/
    base.py         backend contract (CaptureBackend)
    hlae.py         HLAE recording: session cfg, sessions, move   [source-checked, not run in CS2]
  export.py         plan -> 16:9 / 9:16 files                     [tested]
  ffmpeg.py         reframing, H.264 encoding, tool lookup        [tested with real ffmpeg]
  gui/              PySide6 desktop app                           [tested offscreen]
packaging/          PyInstaller spec and notes                    [built on Linux]
.github/workflows/  windows-build.yml                             [not run yet]
docs/DESIGN.md      research, verified facts, open questions
```

## Status

| Part | State |
|---|---|
| Analysis, selection, planning, plan JSON | Done, tested |
| Desktop UI (scan, rounds, players, preview, render, cancel) | Done, tested offscreen |
| 16:9 / 9:16 reframing and encoding | Done, verified with ffmpeg on synthetic video |
| HLAE backend | Written. Recording commands checked against HLAE's CS2 source. Not run against CS2 yet |
| Parsing a real `.dem` | **Not yet verified.** demoparser2 column names are unconfirmed |
| POV selection in CS2 demos | **Not solved.** HLAE has no CS2 POV command. The recording shows the demo's own camera |
| Windows build | Spec builds on Linux. Windows build runs in CI, not yet run |

## Tests

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
```

Integration tests skip automatically when FFmpeg is not installed.
