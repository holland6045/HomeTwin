"""macOS app bundle: structure, baking, and script sanity (built on any OS)."""

import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_build_app_produces_valid_bundle(tmp_path):
    env = {
        "REPO_URL": "https://example.com/repo",
        "CHANNEL": "test-channel",
        "VERSION": "9.9.9",
        "OUT": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run([str(ROOT / "packaging/macos/build_app.sh")], env=env, check=True)
    app = tmp_path / "HomeTwin.app"

    with open(app / "Contents/Info.plist", "rb") as f:
        plist = plistlib.load(f)
    assert plist["CFBundleExecutable"] == "HomeTwin"
    assert plist["CFBundleVersion"] == "9.9.9"
    assert "NSCameraUsageDescription" in plist

    launcher = app / "Contents/MacOS/HomeTwin"
    text = launcher.read_text()
    assert launcher.stat().st_mode & 0o111, "launcher must be executable"
    assert "https://example.com/repo" in text and "test-channel" in text
    assert "__REPO_URL__" not in text and "__CHANNEL__" not in text

    for name in ("Update HomeTwin.command", "Stop HomeTwin.command",
                 "HomeTwin Logs.command", "default-config.yaml"):
        assert (app / "Contents/Resources" / name).exists(), name
    assert "type: camera" in (app / "Contents/Resources/default-config.yaml").read_text()

    # every shipped script parses
    for script in [launcher, *app.glob("Contents/Resources/*.command")]:
        subprocess.run(["bash", "-n", str(script)], check=True)
