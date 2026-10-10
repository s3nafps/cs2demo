# cs2hl: local CS2 highlight generator

Finds the highlight moments in a CS2 demo (multikills, clutches, aces) and exports
them as clips in 16:9 and 9:16, so you don't need an online service.

**Status: early.** The analysis, planning and export pipeline works and is tested.
Parsing a real `.dem` file and recording clips from CS2 are not verified yet. See
[Status](#status) and [docs/DESIGN.md](docs/DESIGN.md).

## Quick start

Requires Python 3.11+ and [FFmpeg](https://ffmpeg.org/) on your PATH.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip install -e ".[dev]"

# 1. Find highlights in a demo and write a plan
cs2hl analyze match.dem                   # -> match.plan.json

# 2. Record each clip in CS2 as <clip id>.mp4 in a folder (capture step, not yet built)

# 3. Export each clip to 16:9 and 9:16
cs2hl export match.plan.json --recordings recordings/ --out output/ \
    --aspect 16x9 9x16 --vertical-mode crop --short-side 1080
```

Useful options:

- `analyze --min-score 6 --max-clips 10 --pre-roll 5 --post-roll 2`
- `export --vertical-mode fit` keeps the full frame over a blurred background
- `export --x-offset 0.6` shifts the 9:16 crop window right, to keep the kill feed
- `export --short-side 1440` or `2160` for higher resolution, `--crf 16` for more quality

## What it detects (v1)

| Kind | Meaning | Example label |
|---|---|---|
| multikill | 2+ kills by one player, each within 10 s of the last | `3K`, `ACE` |
| clutch | last player alive vs N enemies, and the team wins the round | `1v2` |
| team ace | one side kills every enemy alive at round start | `TEAM ACE` |

Bonuses for headshots, noscopes, through smoke, wallbangs and knife kills, plus a
win bonus. The weights live in `cs2hl/scoring.py` and are a first guess to tune.

## Layout

```
cs2hl/
  parse.py      demoparser2 -> models (rounds, kills, rosters)     [not yet verified on a real demo]
  detect.py     multikills, clutches, team aces                    [pure, tested]
  scoring.py    highlight scores and bonuses                       [pure, tested]
  planner.py    clip windows, merging, plan JSON                   [pure, tested]
  ffmpeg.py     16:9 / 9:16 reframing and H.264 encoding           [tested with real ffmpeg]
  export.py     plan -> rendered files                             [tested]
  cli.py        `cs2hl analyze` and `cs2hl export`
tests/          pytest suite (28 tests)
docs/DESIGN.md  research on similar projects, decisions, open questions
```

## Status

| Part | State |
|---|---|
| Detection, scoring, planning, plan JSON | Done, unit-tested |
| 16:9 / 9:16 reframing and encoding | Done, verified with ffmpeg on synthetic video |
| demoparser2 adapter (`parse.py`) | Written and tested with fake parser data. **Not run on a real `.dem` yet**, so column names may need small fixes |
| CS2 capture (recording clips from the game) | Not started. Needs a decision on the backend, see DESIGN.md |
| Local UI | Not started |

## Tests

```bash
.venv/bin/python -m pytest
```

Integration tests skip automatically when FFmpeg is not installed.
