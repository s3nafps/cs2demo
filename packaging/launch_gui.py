"""PyInstaller entry point for the desktop app.

A separate file because the GUI modules use relative imports and cannot run as a top-level
script. The command-line arguments go to the app unchanged, so a .dem path that Windows
passes ("Open with CS2 Highlights", or a file dropped on the exe) opens that demo.

``CS2Highlights.exe --self-check REPORT [DEMO]`` is the build's smoke test. It loads the
libraries demoparser2 imports from native code, which PyInstaller cannot see, optionally scans
DEMO, writes "ok" or the traceback to REPORT (a windowed exe has no console) and sets the
exit code.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

SELF_CHECK = "--self-check"


def self_check(report: Path, demo: Path | None) -> int:
    try:
        import pandas
        import polars
        import psutil
        import pyarrow

        importlib.import_module("cs2hl.gui.app")  # the whole GUI import graph

        frame = polars.DataFrame({"tick": [1, 2, 3]}).to_pandas()  # the conversion demoparser2 does
        if frame["tick"].tolist() != [1, 2, 3]:
            raise RuntimeError(f"polars to pandas gave {frame!r}")
        lines = [f"{m.__name__} {m.__version__}" for m in (pandas, polars, pyarrow, psutil)]
        if demo is not None:
            from cs2hl.scan import scan_demo

            scan = scan_demo(demo)
            lines.append(f"{scan.demo_name}: {scan.map_name}, {len(scan.rounds)} rounds, {len(scan.players)} players")
        report.write_text("ok\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return 0
    except BaseException:
        report.write_text("failed\n" + traceback.format_exc(), encoding="utf-8")
        return 1


def run(argv: list[str]) -> int:
    if argv[:1] == [SELF_CHECK] and len(argv) in (2, 3):
        return self_check(Path(argv[1]), Path(argv[2]) if len(argv) == 3 else None)
    from cs2hl.gui.app import main

    return main(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
