#!/usr/bin/env python3
"""Install Ollama (official HTTPS) + pull the GrantLens assessment model.

Markers / external testers can run:

    python _install_ollama.py

What it does:
  1) Checks whether Ollama is already available
  2) If not, downloads the official installer over HTTPS from ollama.com
  3) Runs the installer (Windows GUI / Unix install.sh)
  4) Waits until the local API answers on http://127.0.0.1:11434
  5) Pulls qwen3:8b (override with GRANTLENS_MODEL)

Then start the app with:

    python _start_ollama.py
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = os.environ.get("GRANTLENS_MODEL", "qwen3:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# Official Ollama distribution endpoints (HTTPS only)
INSTALL_SH = "https://ollama.com/install.sh"
INSTALL_PS1 = "https://ollama.com/install.ps1"
WINDOWS_SETUP = "https://ollama.com/download/OllamaSetup.exe"


def log(msg: str = ""):
    print(msg, flush=True)


def which_ollama() -> str | None:
    return shutil.which("ollama")


def api_ok(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def download(url: str, dest: Path) -> None:
    log(f"  Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "GrantLens-setup/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    log(f"  Saved → {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")


def run(cmd: list[str] | str, shell: bool = False) -> int:
    log(f"  $ {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    return subprocess.call(cmd, shell=shell)


def install_windows() -> None:
    """Prefer official install.ps1; fall back to OllamaSetup.exe."""
    tmp = Path(tempfile.mkdtemp(prefix="grantlens-ollama-"))
    ps1 = tmp / "install.ps1"
    try:
        download(INSTALL_PS1, ps1)
        # Official one-liner equivalent, but from a saved file for auditability
        code = run([
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command",
            f"& '{ps1}'",
        ])
        if code == 0:
            return
        log("  install.ps1 returned non-zero — trying OllamaSetup.exe …")
    except Exception as e:
        log(f"  install.ps1 failed ({e}) — trying OllamaSetup.exe …")

    exe = tmp / "OllamaSetup.exe"
    download(WINDOWS_SETUP, exe)
    log("  Launching Windows installer (complete any on-screen prompts) …")
    code = run([str(exe)])
    if code != 0:
        raise SystemExit(
            f"Ollama installer exited with code {code}. "
            "Install manually from https://ollama.com/download/windows then re-run this script."
        )


def install_unix() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="grantlens-ollama-"))
    sh = tmp / "install.sh"
    download(INSTALL_SH, sh)
    sh.chmod(sh.stat().st_mode | 0o755)
    # Pipe through sh like the official curl | sh, but from a downloaded file
    code = run(["sh", str(sh)])
    if code != 0:
        raise SystemExit(
            f"Ollama install.sh exited with code {code}. "
            "Install manually: curl -fsSL https://ollama.com/install.sh | sh"
        )


def refresh_path_hint() -> None:
    if platform.system() == "Windows":
        # Common per-user install location after OllamaSetup / install.ps1
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
        if local.is_dir():
            os.environ["PATH"] = str(local) + os.pathsep + os.environ.get("PATH", "")


def wait_for_api(seconds: int = 120) -> bool:
    log(f"Waiting for Ollama API at {OLLAMA_URL} (up to {seconds}s) …")
    deadline = time.time() + seconds
    while time.time() < deadline:
        refresh_path_hint()
        if api_ok():
            log("  API is up.")
            return True
        # Try starting the app / serve if CLI exists but API is down
        exe = which_ollama()
        if exe and platform.system() == "Windows":
            # Opening ollama.exe usually starts the tray service
            try:
                subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        time.sleep(2)
    return False


def pull_model() -> None:
    refresh_path_hint()
    exe = which_ollama()
    if not exe:
        # Last resort: HTTP pull is not exposed the same way — require CLI
        raise SystemExit(
            "Ollama CLI not found on PATH after install. "
            "Close this terminal, open a new one, then run:\n"
            f"  ollama pull {MODEL}\n"
            "Or re-open this script after adding Ollama to PATH."
        )
    log(f"Pulling model {MODEL} (first time may download several GB) …")
    code = run([exe, "pull", MODEL])
    if code != 0:
        raise SystemExit(f"`ollama pull {MODEL}` failed (exit {code}).")


def main() -> int:
    log("GrantLens — Ollama setup")
    log("=" * 48)
    log(f"Platform : {platform.system()} {platform.machine()}")
    log(f"Model    : {MODEL}")
    log(f"API      : {OLLAMA_URL}")
    log()
    log("This script downloads ONLY from official HTTPS endpoints:")
    log(f"  {INSTALL_PS1}")
    log(f"  {WINDOWS_SETUP}")
    log(f"  {INSTALL_SH}")
    log()

    if which_ollama() and api_ok():
        log("Ollama is already installed and the API is reachable.")
    else:
        if which_ollama() and not api_ok():
            log("Ollama CLI found, but API is not up yet — will try to start / wait.")
        else:
            ans = input("Ollama not detected. Install now from ollama.com? [Y/n] ").strip().lower()
            if ans in ("n", "no"):
                log("Aborted. Install manually from https://ollama.com then re-run.")
                return 1
            if platform.system() == "Windows":
                install_windows()
            elif platform.system() in ("Linux", "Darwin"):
                install_unix()
            else:
                raise SystemExit(f"Unsupported OS: {platform.system()}")

        if not wait_for_api():
            raise SystemExit(
                "Timed out waiting for Ollama. Open the Ollama app once, then re-run:\n"
                "  python _install_ollama.py"
            )

    pull_model()
    log()
    log("Done.")
    log("Start GrantLens with the real model:")
    log("  python _start_ollama.py")
    log("Then open http://127.0.0.1:8000")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as e:
        log(f"HTTPS download failed: {e}")
        log("Check your network, or install manually from https://ollama.com")
        raise SystemExit(1) from e
    except KeyboardInterrupt:
        log("\nInterrupted.")
        raise SystemExit(130)
