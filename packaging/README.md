# Packaging: the Windows installer

The release is one file, `CS2Highlights-Setup-<version>.exe`. Running it installs the app
with no admin rights needed. After that, the app runs from the Start Menu, and you can
open a `.dem` file in it from Explorer.

| File | What it is |
|---|---|
| `cs2hl.spec` | PyInstaller spec. Builds `dist/CS2Highlights/` as a windowed one-folder app (`CS2Highlights.exe` and `_internal/`) |
| `launch_gui.py` | Entry point. Passes the command line to `cs2hl.gui.app.main`, so a `.dem` path given by Windows opens that demo |
| `installer.iss` | Inno Setup 6 script. Wraps the folder, FFmpeg and shortcuts into the setup exe |
| `../.github/workflows/windows-build.yml` | CI job that builds, smoke-tests and uploads both |

## What the installer does

- Asks "only for me" (the default, installs to `%LOCALAPPDATA%\Programs\CS2 Highlights`, no
  admin) or "all users" (installs to `C:\Program Files\CS2 Highlights`, needs admin).
- Installs the app folder plus `ffmpeg.exe` and `ffprobe.exe` from the gyan.dev
  "release essentials" build. The app looks for both next to its exe. The FFmpeg licence
  (GPL v3) and build notes go in `licenses\`.
- Adds a Start Menu shortcut. Offers a desktop shortcut (ticked by default) and a
  `.dem` association (not ticked). The association adds CS2 Highlights to "Open with"
  and an "Open with CS2 Highlights" item to the right-click menu. It does not take over
  double-click, so an existing default such as CS Demo Manager stays.
- Offers to launch the app at the end. The finish page says what else is needed.
- Uninstalls from Settings > Apps. That removes every installed file, the shortcuts, the
  `.dem` entries and the folder. Files you added to the folder yourself stay. The app's
  settings (`HKCU\Software\cs2hl`) and the videos you made also stay.

**Not installed:** Counter-Strike 2 (install it through Steam) and HLAE. Get HLAE from
[advancedfx.org](https://www.advancedfx.org/) or its
[releases page](https://github.com/advancedfx/advancedfx/releases/latest). The app looks in
the usual folders (Program Files, `%LOCALAPPDATA%\cs2hl\HLAE`, CS Demo Manager's copy,
and others), or you choose `HLAE.exe` in the app. The app can point HLAE at the bundled
`ffmpeg.exe`.

## Making a release

1. Set the same version in `cs2hl/__init__.py` and `pyproject.toml`. CI fails if they differ.
2. Push a tag `v<version>`, for example `git tag v0.2.0 && git push origin v0.2.0`. CI also
   fails if the tag and the version differ. You can instead run the **windows-build**
   workflow by hand (Actions > windows-build > Run workflow).
3. Download the `CS2Highlights-Setup-<version>` artifact from the run. Runs also keep a
   `CS2Highlights-<version>-app-folder` artifact: the bare PyInstaller folder, with no FFmpeg.

The workflow runs on `windows-latest` with Python 3.12. In order, it:

1. Installs `.[gui]` and `pyinstaller==6.22.3`, and reads the version.
2. Builds the app folder.
3. Runs `CS2Highlights.exe --self-check`, then starts the window offscreen and checks it
   is still up after 20 s.
4. Downloads the FFmpeg zip and checks its SHA-256 against gyan.dev.
5. Installs Inno Setup 6.7.1 from Chocolatey if `ISCC.exe` is missing, and compiles the
   installer.

## Building locally on Windows

You need Python 3.12 (64-bit) and [Inno Setup](https://jrsoftware.org/isdl.php) 6.3 or
later. From the repository root, in PowerShell:

```powershell
python -m pip install -e ".[gui]" pyinstaller==6.22.3
pyinstaller packaging\cs2hl.spec --noconfirm --distpath dist --workpath build\pyinstaller
Start-Process .\dist\CS2Highlights\CS2Highlights.exe -ArgumentList "--self-check", "check.txt" -Wait; Get-Content check.txt
# Unzip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip, then copy
# bin\ffmpeg.exe, bin\ffprobe.exe, LICENSE -> FFmpeg-LICENSE.txt and README.txt ->
# FFmpeg-README.txt into one folder, for example build\staging.
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.2.0 "/DStagingDir=$PWD\build\staging" packaging\installer.iss
```

The setup exe lands in `dist\`. `--self-check REPORT [DEMO]` writes `ok` or a traceback to
REPORT. Given a demo, it also scans it, which tests the whole parser inside the frozen app.

## Notes

- The spec bundles only the Qt modules that `cs2hl` imports. It leaves out pyarrow's Flight
  and S3 parts, Qt's virtual keyboard and PDF plugins, and headers and sources. If the app
  starts to use one of them, edit `unused_file()` in the spec. demoparser2 imports polars
  and pyarrow from native code, so they are listed as hidden imports.
- The installer is not code-signed, so SmartScreen will warn on first run until it is.
- FFmpeg is GPL v3. Shipping it means offering its source. The bundled README names the
  exact FFmpeg commit, and gyan.dev hosts the build.
