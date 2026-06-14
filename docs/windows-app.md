# HomeTwin on Windows — packaging and the update loop

Same thin-launcher model as `HomeTwin.app` (docs/macos-app.md), shaped
for the test-and-iterate phase: **relaunching the app is the update**.
The Python environment lives under `%LOCALAPPDATA%\HomeTwin`, so updates
are a pip reinstall from git — no rebuilding, no re-downloading.

## Build (once, on any machine — bash, not Windows)

```bash
REPO_URL=https://github.com/holland6045/HomeTwin \
CHANNEL=claude/apartment-tracking-cv-app-9n823r \
packaging/windows/build_win.sh
```

The script zips the package itself as
`dist/HomeTwin-win-<date>-<commit>.zip` — the suffix names the exact
source build, so zips floating around Downloads folders stay traceable
(`version.txt` inside matches). Copy the zip to the PC.

`CHANNEL` is the git branch the launcher tracks — point it at the
development branch while we iterate, at `main` when things settle.

## First launch (on the PC)

1. Unzip `HomeTwin-win` anywhere (Desktop is fine — paths are relative).
2. Double-click **`HomeTwin.bat`**. SmartScreen may warn once; choose
   "More info → Run anyway".
3. The launcher creates `%LOCALAPPDATA%\HomeTwin\venv`, pip-installs
   `hometwin[vision,ml]` from the channel branch, writes the default webcam
   config, starts the tracker hidden, and opens the dashboard at
   `http://127.0.0.1:8080/`. First run takes a couple of minutes (OpenCV
   download); later launches are seconds.

Needs (the launcher checks and tells you if either is missing):

- **Python 3** from python.org — keep the "py launcher" option checked.
- **Git for Windows** (git-scm.com) — updates install straight from the repo.

## The update loop while we iterate

- I push to the channel branch.
- You re-run **`HomeTwin.bat`** — it compares the remote branch head to
  the installed commit and reinstalls only when they differ, then
  restarts the tracker. That's the whole loop.
- Impatient mid-session: double-click **`Update HomeTwin.bat`**.
- Pin a version while debugging hardware:
  `echo false > %LOCALAPPDATA%\HomeTwin\autoupdate`
- Switch channels: `echo main > %LOCALAPPDATA%\HomeTwin\channel`

## Where things live

| What | Where |
|---|---|
| Config (edit me) | `%LOCALAPPDATA%\HomeTwin\config.yaml` |
| venv + installed commit | `%LOCALAPPDATA%\HomeTwin\` |
| Logs | `%LOCALAPPDATA%\HomeTwin\logs\` (`HomeTwin Logs.bat` tails the main one) |
| Stop the tracker | `Stop HomeTwin.bat` |
| CLI access | `%LOCALAPPDATA%\HomeTwin\venv\Scripts\hometwin.exe ...` |

The CLI from the launcher's venv is the same toolbox as always —
`webcam-setup`, `make-anchor`, `make-tag`, `snapshot-map` all work
against the launcher-managed install.

## Doing more than watching the webcam

A fresh install only runs ArUco tag detection, so with no printed tags in
view nothing is tracked — just the video. Two one-click paths in the
dashboard's **⚙ Settings** get you to real tracking:

- **Enable AI detection** (per camera): downloads RT-DETR (~80 MB, once)
  and composes it onto the camera's detector via the `multi` detector, so
  it tracks ArUco tags **and** people/objects (your phone is a COCO
  class) at the same time. Person detections drive any motion zones you
  draw on the map and appear in Home Assistant as PIRs.
- **Make a tag**: enter an id/label and click *Open / print* — a printable
  ArUco SVG opens in a new tab (Ctrl-P at 100%). Tape it to your keys and
  put `tags: ["aruco:<id>"]` on that item in `config.yaml`.
- **Detect modes**: probes the webcam's real resolutions/fps; pick one and
  Apply (also `hometwin webcam-probe` from the CLI).

CLI equivalents still work against the launcher venv: `get-model rtdetr`,
`make-tag --id 7`, `webcam-probe`. GPU: reinstall with `[vision,ml-dml]`
(DirectML) or `[vision,ml-cuda]` (NVIDIA) for fast inference.

## Remote debugging without remote access

The dashboard sidebar has a **Download diagnostics** link
(`/debug/bundle`): one zip with health/version info, overlay and item
state, per-camera detection state, and the latest annotated frame from
every camera. When something misbehaves, grab the bundle and hand it to
whoever is debugging — it answers "what does the tracker think is
happening" without anyone needing access to the machine.

## Troubleshooting

- Window flashes and closes / "does nothing": run `HomeTwin Logs.bat`;
  most common is camera-in-use (Teams/Zoom holding the webcam) or
  Windows camera privacy settings (Settings → Privacy → Camera →
  "Let desktop apps access your camera").
- `cannot open camera 0`: edit the config's `device:` index, or run
  `%LOCALAPPDATA%\HomeTwin\venv\Scripts\hometwin.exe webcam-test --device 1`.
- Wedged install: delete `%LOCALAPPDATA%\HomeTwin\venv` and relaunch —
  the venv is fully disposable; config and state survive.
