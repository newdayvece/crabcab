#!/usr/bin/env python3
"""crabcab doctor - checks your machine is ready to run the bridge. prints no secrets."""
import os
import sys
import json
import shutil
import pathlib
import platform


def status(ok, label, detail=""):
    print(f"  {'ok  ' if ok else 'fail'} {label}" + (f" - {detail}" if detail else ""))


print("\ncrabcab doctor - is your machine ready?\n")

print(f"  -  os: {platform.system()} ({platform.machine()})")
if platform.system() == "Windows" and os.environ.get("PSModulePath"):
    _shell = "powershell"
else:
    _shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"
print(f"  -  shell: {_shell}")
print()

ready = True

py_ok = sys.version_info >= (3, 7)
status(py_ok, f"python {sys.version.split()[0]}", "" if py_ok else "need 3.7+")
ready &= py_ok

curl = shutil.which("curl")
status(bool(curl), "curl", curl or "NOT FOUND - install curl")
ready &= bool(curl)

claude = shutil.which("claude")
status(bool(claude), "claude (claude code cli)", claude or "NOT FOUND - install Claude Code")
ready &= bool(claude)

auth = pathlib.Path.home() / ".codex" / "auth.json"
if not auth.exists():
    status(False, "codex login",
           "no ~/.codex/auth.json - install Codex CLI, run `codex`, choose 'Sign in with ChatGPT'")
    ready = False
else:
    try:
        d = json.loads(auth.read_text())
        has_tok = bool((d.get("tokens") or {}).get("access_token"))
        sub_mode = d.get("OPENAI_API_KEY") is None
        status(has_tok, "codex login found", d.get("auth_mode") or "")
        status(sub_mode, "subscription mode (not api-key billing)",
               "" if sub_mode else "you're in api-key mode; crabcab targets subscription mode")
        ready &= has_tok
    except Exception as e:
        status(False, "codex auth.json unreadable", str(e)[:80])
        ready = False

print()
print("  all set - go to step 1!" if ready else "  -> fix the fail items above, then run me again.")
print()
