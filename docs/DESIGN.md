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
