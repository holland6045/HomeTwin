"""App packages (macOS bundle, Windows folder): structure, baking, script
sanity — built and checked on any OS."""

import plistlib
import shutil
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


def test_build_win_produces_valid_package(tmp_path):
    env = {
        "REPO_URL": "https://example.com/repo",
        "CHANNEL": "test-channel",
        "VERSION": "9.9.9",
        "OUT": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run([str(ROOT / "packaging/windows/build_win.sh")], env=env, check=True)
    pkg = tmp_path / "HomeTwin-win"

    for name in ("HomeTwin.bat", "Update HomeTwin.bat", "Stop HomeTwin.bat",
                 "HomeTwin Logs.bat", "launcher.ps1", "update.ps1", "stop.ps1",
                 "logs.ps1", "default-config.yaml", "version.txt"):
        assert (pkg / name).exists(), name
    assert (pkg / "version.txt").read_text().strip() == "9.9.9"
    assert "type: camera" in (pkg / "default-config.yaml").read_text()

    for ps1 in ("launcher.ps1", "update.ps1"):
        text = (pkg / ps1).read_text()
        assert "https://example.com/repo" in text and "test-channel" in text
        assert "__REPO_URL__" not in text and "__CHANNEL__" not in text
        # Windows PowerShell 5.1 is the baseline; ?? and ?. are pwsh 7+
        assert " ?? " not in text and "?." not in text

    # batch files are what users double-click: CRLF for cmd.exe, relative
    # script paths so the folder can live anywhere
    for bat in pkg.glob("*.bat"):
        raw = bat.read_bytes()
        assert b"\r\n" in raw and not raw.replace(b"\r\n", b"\n").count(b"\r"), bat.name
        assert b"%~dp0" in raw, bat.name

    # ps1 scripts must parse when a PowerShell is available to check them
    pwsh = shutil.which("pwsh")
    if pwsh:
        for script in pkg.glob("*.ps1"):
            check = (
                "$e=$null;"
                f"[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$null,[ref]$e)|Out-Null;"
                "if($e){Write-Error ($e -join \"`n\");exit 1}"
            )
            subprocess.run([pwsh, "-NoProfile", "-Command", check], check=True)
