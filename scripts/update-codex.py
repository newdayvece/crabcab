#!/usr/bin/env python3
"""crabcab: check whether Codex needs updating, and how - WITHOUT changing anything.

This only DETECTS and RECOMMENDS. It installs nothing, runs no update, and touches no
files. It prints the right update command for your install method, plus links so you can
verify the update yourself before running it.
"""
import os
import shutil
import platform
import subprocess
import pathlib


def codex_version():
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5).stdout
        for tok in out.replace("codex-cli", "").split():
            if tok and all(c.isdigit() or c == "." for c in tok):
                return tok
    except Exception:
        pass
    return None


def detect_method(path):
    """return (method, update_command, note) for how Codex was installed."""
    system = platform.system()
    real = os.path.realpath(path).lower() if path else ""

    if "/caskroom/" in real:
        return ("homebrew cask", "HOMEBREW_NO_AUTO_UPDATE=1 brew upgrade --cask codex",
                "the HOMEBREW_NO_AUTO_UPDATE=1 flag bumps only codex and skips brew's full "
                "auto-update. don't run codex's own self-updater either - let brew own it.")
    if "/cellar/" in real:
        return ("homebrew formula", "HOMEBREW_NO_AUTO_UPDATE=1 brew upgrade codex",
                "the HOMEBREW_NO_AUTO_UPDATE=1 flag bumps only codex and skips brew's full "
                "auto-update. don't run codex's own self-updater either - let brew own it.")

    try:
        npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=5).stdout.strip()
        if npm_root and (pathlib.Path(npm_root) / "@openai" / "codex").exists():
            return ("npm (global)", "npm update -g @openai/codex", "")
    except Exception:
        pass

    if system == "Windows":
        if shutil.which("winget"):
            return ("winget (likely)", "winget upgrade OpenAI.Codex",
                    "Confirm the exact id first with:  winget list codex")
        if shutil.which("scoop"):
            return ("scoop (likely)", "scoop update codex", "")

    return ("standalone binary", "codex update",
            "Not managed by a package manager - Codex's built-in updater is fine here.")


def main():
    print("\ncrabcab: codex update check  (nothing will be changed)\n")
    path = shutil.which("codex")
    ver = codex_version()
    print(f"  os:             {platform.system()} ({platform.machine()})")
    print(f"  codex found:    {path or 'NOT on PATH'}")
    print(f"  codex version:  {ver or 'unknown'}")

    if not path:
        print("\n  Codex isn't on your PATH. Install the Codex CLI and sign in with ChatGPT first.\n")
        return

    method, cmd, note = detect_method(path)
    print(f"  install method: {method}")
    print("\n  if a model says it needs a newer codex, the right update for your setup is:\n")
    print(f"      {cmd}\n")
    if note:
        print(f"  note: {note}\n")

    print("  sources to verify the update yourself (what changed, that it's legit):")
    print("    - codex releases + changelog:   https://github.com/openai/codex/releases")
    print("    - openai developers on x:        https://x.com/OpenAIDevs")
    print("    - same, via nitter mirror:       https://nitter.net/OpenAIDevs")
    print("    - claude code releases:          https://github.com/anthropics/claude-code/releases")
    print("\n  this script ran nothing. copy the command above only if you choose to update.\n")


if __name__ == "__main__":
    main()
