# Packaging the desktop app

`cs2hl-gui.spec` builds `cs2hl-gui.exe`, a one-file windowed app started from `launch_gui.py`.

Build on Windows, from the repo root:

    python -m pip install -e ".[gui]" pyinstaller==6.22.3
    pyinstaller packaging\cs2hl-gui.spec --noconfirm --distpath dist --workpath build
    copy ffmpeg.exe dist\

Bundled: the GUI, PySide6, demoparser2, pandas. Not bundled: HLAE, and ffmpeg.exe, which must sit next to the exe.
The user installs HLAE (set in the app) and Counter-Strike 2. The `windows-build` workflow runs these steps and uploads `dist/`.

Not verified: the Windows build and run (Linux offscreen only), parsing a real `.dem` in the frozen build, and the third-party ffmpeg URL.
