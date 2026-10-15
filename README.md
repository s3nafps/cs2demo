# CS2 Highlights (cs2hl)

Pick a CS2 demo and a player, and get that player's highlights as one video, separate
clips, or both, in 16:9 and 9:16. Everything runs on your Windows PC: the app finds the
highlights, records them in CS2 through HLAE, and edits the videos with FFmpeg.

**Status: 0.2, not yet run in CS2.** Parsing and highlight picking are checked on two
real demos. Recording through HLAE is written from HLAE's and CS Demo Manager's source
and has not been run on a real PC yet. See [Status](#status).

## Install

1. Install [HLAE](https://github.com/advancedfx/advancedfx/releases) (the app does not
   bundle it). Steam and Counter-Strike 2 must be installed.
2. Run `CS2Highlights-Setup-<version>.exe`. It installs the app with FFmpeg, adds a Start
   menu shortcut, and can optionally open `.dem` files with the app. No admin rights needed.

The installer is built by the `windows-build` GitHub Actions workflow (see
[packaging/README.md](packaging/README.md)).

## Use

1. **Open a demo** (button, drag and drop, or double-click a `.dem` if you chose that in setup).
   The app scans it in about a second.
2. **Pick the player.** Tick "This is me" once, and the app picks you in every later demo.
3. **Choose the output:** One video, Separate clips, or Both, and 16:9 and/or 9:16.
4. **Generate.** The app starts CS2 through HLAE, records each highlight, closes CS2, and
   exports the videos. Open folder shows the result.

Keep the CS2 window open while it records, and don't play CS2 at the same time: the game
runs with `-insecure`, so it can't join VAC servers in that session.

The Advanced tab has the round filter, kills needed per round, clutches, pre- and post-roll,
quality and capture size. The Setup tab shows what the app found (Steam, CS2, HLAE, FFmpeg)
and lets you point it at a folder by hand.

## What counts as a highlight

| Highlight | Rule | Label |
|---|---|---|
| Multikill | 2 or more kills by the player in one round (setting: kills per round) | `2K`, `3K`, `4K`, `ACE` |
| Clutch | last player alive against N enemies, and the round is won | `1v2`, or `3K + 1v2` |

Each kill gets a window (3 s before, 2 s after by default). Windows close together merge,
and a round's pieces are joined with cuts, so a 4K spread over 40 s becomes a 20 s clip.
Scores are in `cs2hl/scoring.py` and only decide the order when you limit the clip count.

## How recording works

The app writes a start script to `<CS2>\game\csgo\cfg\cs2hl_boot.cfg` and launches
`HLAE.exe -customLoader -noGui -autoStart ...` with CS2 set to `-insecure -sw -w W -h H
+exec cs2hl_boot`. The script sets up HLAE's FFmpeg recorder, hides the HUD except the kill
feed, filters the kill feed to the chosen player, and schedules every piece on demo ticks
with `mirv_cmd addAtTick`: seek, point the camera (`spec_mode 1`, `spec_player <slot>`),
record, stop. It ends with `quit`; the app waits for `cs2.exe` to exit, collects the
pieces, and exports. Details and sources are in [docs/DESIGN.md](docs/DESIGN.md).

## Command line

```bash
cs2hl analyze match.dem --players 76561198000000000 --min-kills 2
cs2hl export match.plan.json --recordings recordings/ --output both --aspect 16x9 9x16
```

## Status

| Part | State |
|---|---|
| Parsing (`parse.py`) | Checked on a matchmaking and a FACEIT demo: rounds, score and every player's kills match the game's own counters |
| Highlight picking and clip cuts | Tested; checked on both real demos |
| Export (clips, montage, both; 16:9 and 9:16) | Tested with real FFmpeg on synthetic video, including sound sync across cuts |
| Desktop app | Tested offscreen on Linux; not yet run on Windows |
| Steam, CS2 and HLAE discovery, setup checks | Tested with fakes; not yet run on Windows |
| Recording through HLAE | Written from source; **not yet run in CS2** |
| Installer | Built by CI on Windows; see packaging/README.md |

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[gui,dev]"
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
```

Real-demo tests run when `CS2HL_DEMO_DIR` points at a folder with `mm.dem` and
`faceit.dem` (the public test demos from https://ndownloader.figshare.com/files/52456259 and
https://ndownloader.figshare.com/files/52455215).
