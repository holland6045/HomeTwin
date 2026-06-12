# HomeTwin.app — macOS packaging and the update loop

The app is a thin native launcher built for the test-and-iterate phase:
**relaunching the app is the update**. The Python environment lives outside
the bundle, so updates are a pip reinstall from git — no rebuilding, no
re-downloading the app, no notarization churn while we iterate.

## Build (once, on any machine)

```bash
REPO_URL=https://github.com/holland6045/RGBDesk \
CHANNEL=claude/apartment-tracking-cv-app-9n823r \
packaging/macos/build_app.sh

zip -r HomeTwin.app.zip dist/HomeTwin.app   # AirDrop / copy to the Mac
```

`CHANNEL` is the git branch the app tracks — point it at the development
branch while we iterate, at `main` when things settle.

## First launch (on the Mac)

1. Unzip, drag `HomeTwin.app` to Applications.
2. **Right-click → Open** the first time (ad-hoc signature; Gatekeeper
   asks once).
3. The launcher creates `~/Library/Application Support/HomeTwin/venv`,
   pip-installs `hometwin[vision,ml]` from the channel branch, writes the
   default webcam config, starts the tracker, and opens the dashboard.
   First run takes a couple of minutes (OpenCV download); later launches
   are seconds.
4. macOS will ask for camera permission on the first frame grab.

Needs: Apple Command Line Tools (`xcode-select --install`) for `python3`
and the pip git install.

## The update loop while we iterate

- I push to the channel branch.
- You quit and **reopen the app** — it compares the remote branch head to
  the installed commit and reinstalls only when they differ, then
  restarts the tracker. That's the whole loop.
- Impatient mid-session: double-click
  `HomeTwin.app/Contents/Resources/Update HomeTwin.command`.
- Pin a version while debugging hardware: `echo false >
  "~/Library/Application Support/HomeTwin/autoupdate"`.
- Switch channels: `echo main > "~/Library/Application Support/HomeTwin/channel"`.

## Where things live

| What | Where |
|---|---|
| Config (edit me) | `~/Library/Application Support/HomeTwin/config.yaml` |
| venv + installed commit | `~/Library/Application Support/HomeTwin/` |
| Logs | `~/Library/Logs/HomeTwin/hometwin.log` (`HomeTwin Logs.command` tails it) |
| Stop the tracker | `Stop HomeTwin.command` in the bundle's Resources |
| CLI access | `~/Library/Application\ Support/HomeTwin/venv/bin/hometwin ...` |

The CLI from the app's venv is the same toolbox as always —
`webcam-setup`, `make-anchor`, `make-tag`, `snapshot-map` all work against
the app-managed install.

## Troubleshooting

- App "does nothing": check the log; most common is camera permission
  (System Settings → Privacy → Camera) or another app holding the webcam.
- `cannot open camera 0`: edit the config's `device:` index, or run
  `.../venv/bin/hometwin webcam-test --device 1`.
- Wedged install: delete `~/Library/Application Support/HomeTwin/venv`
  and relaunch — the venv is fully disposable; config and state survive.
