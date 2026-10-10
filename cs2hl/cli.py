"""Command-line entry point: ``cs2hl analyze`` and ``cs2hl export``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ffmpeg import EncodeSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cs2hl", description="Generate CS2 demo highlights.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Find highlights in a .dem file and write a plan.")
    analyze.add_argument("demo", type=Path, help="Path to the .dem file")
    analyze.add_argument("-o", "--output", type=Path, help="Plan JSON path (default: <demo>.plan.json)")
    analyze.add_argument("--min-score", type=float, default=5.0, help="Drop highlights below this score")
    analyze.add_argument("--pre-roll", type=float, default=4.0, help="Seconds kept before the moment")
    analyze.add_argument("--post-roll", type=float, default=2.0, help="Seconds kept after the moment")
    analyze.add_argument("--streak-gap", type=float, default=10.0, help="Max seconds between kills in one multikill")
    analyze.add_argument("--max-clips", type=int, help="Keep only the N highest-scoring clips")
    analyze.add_argument("--players", type=int, nargs="+", metavar="STEAMID",
                         help="Only highlights where these players (steamid64) are the POV")
    analyze.add_argument("--rounds", type=int, nargs="+", metavar="N",
                         help="Only highlights from these round numbers")

    export = sub.add_parser("export", help="Render a plan's clips into 16:9 and/or 9:16 files.")
    export.add_argument("plan", type=Path, help="Plan JSON written by 'analyze'")
    export.add_argument("--recordings", type=Path, required=True, help="Dir with <clip id>.<ext> recordings")
    export.add_argument("--out", type=Path, default=Path("output"), help="Output directory")
    export.add_argument("--aspect", nargs="+", choices=["16x9", "9x16"], default=["16x9", "9x16"])
    export.add_argument("--vertical-mode", choices=["crop", "fit"], default="crop")
    export.add_argument("--x-offset", type=float, default=0.0, help="9:16 crop position, -1 (left) to 1 (right)")
    export.add_argument("--short-side", type=int, default=1080, help="1080, 1440 or 2160")
    export.add_argument("--crf", type=int, default=17, help="H.264 quality (lower = better, larger)")
    export.add_argument("--preset", default="slow", help="x264 preset (slower = smaller for the same quality)")

    return parser


def cmd_analyze(args: argparse.Namespace) -> int:
    from .parse import load_demo
    from .pipeline import HighlightOptions, plan_highlights
    from .planner import save_plan
    from .selection import Selection

    if not args.demo.is_file():
        print(f"error: demo not found: {args.demo}", file=sys.stderr)
        return 2

    data = load_demo(args.demo)
    selection = Selection.of(players=args.players, rounds=args.rounds)
    plan = plan_highlights(
        data,
        selection,
        HighlightOptions(
            streak_gap_s=args.streak_gap,
            pre_roll_s=args.pre_roll,
            post_roll_s=args.post_roll,
            min_score=args.min_score,
            max_clips=args.max_clips,
        ),
    )

    output = args.output or args.demo.with_name(args.demo.stem + ".plan.json")
    save_plan(plan, output)

    meta = data.meta
    print(f"{meta.demo_name} | map {meta.map_name} | {len(data.rounds)} rounds | {len(data.kills)} kills")
    if not plan.clips:
        print("No clips passed the score threshold.")
    for clip in plan.clips:
        print(
            f"  {clip.id}  {clip.start_s:8.1f}s-{clip.end_s:8.1f}s  "
            f"{clip.pov_name:<20.20}  {' + '.join(clip.labels):<12}  score {clip.score:5.1f}  "
            f"{', '.join(clip.tags)}"
        )
    print(f"Plan written to {output}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import export_plan
    from .planner import load_plan

    plan = load_plan(args.plan)
    settings = EncodeSettings(short_side=args.short_side, crf=args.crf, preset=args.preset)
    results = export_plan(
        plan,
        args.recordings,
        args.out,
        args.aspect,
        settings=settings,
        vertical_mode=args.vertical_mode,
        x_offset=args.x_offset,
    )

    failed = 0
    for r in results:
        if r.error:
            failed += 1
            print(f"FAILED {r.clip_id} {r.aspect}: {r.error}", file=sys.stderr)
        else:
            print(f"ok     {r.output}")
    print(f"{len(results) - failed} exported, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "export":
        return cmd_export(args)
    return 2
