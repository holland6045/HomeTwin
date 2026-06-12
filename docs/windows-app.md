# HomeTwin on Windows — packaging and the update loop

Same thin-launcher model as `HomeTwin.app` (docs/macos-app.md), shaped
for the test-and-iterate phase: **relaunching the app is the update**.
The Python environment lives under `%LOCALAPPDATA%\HomeTwin`, so updates
are a pip reinstall from git — no rebuilding, no re-downloading.

## Build (once, on any machine — bash, not Windows)

```bash
REPO_URL=https://github.com/holland6045/RGBDesk \
CHANNEL=claude/apartment-tracking-cv-app-9n823r \
packaging/windows/build_win.sh

(cd dist && zip -r HomeTwin-win.zip HomeTwin-win)   # copy to the PC
```

`CHANNEL` is the git branch the launcher tracks — point it at the
development branch while we iterate, at `main` when things settle.

## First launch (on the PC)

1. Unzip `HomeTwin-win` anywhere (Desktop is fine — paths are relative).
2. Double-click **`HomeTwin.bat`**. SmartScreen may warn once; choose
   "More info → Run anyway".
3. The launcher creates `%LOCALAPPDATA%\HomeTwin\venv`, pip-installs
   `hometwin[vision]` from the channel branch, writes the default webcam
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

## Troubleshooting

- Window flashes and closes / "does nothing": run `HomeTwin Logs.bat`;
  most common is camera-in-use (Teams/Zoom holding the webcam) or
  Windows camera privacy settings (Settings → Privacy → Camera →
  "Let desktop apps access your camera").
- `cannot open camera 0`: edit the config's `device:` index, or run
  `%LOCALAPPDATA%\HomeTwin\venv\Scripts\hometwin.exe webcam-test --device 1`.
- Wedged install: delete `%LOCALAPPDATA%\HomeTwin\venv` and relaunch —
  the venv is fully disposable; config and state survive.
