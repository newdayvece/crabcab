#!/usr/bin/env python3
"""crabcab cost: how much of your weekly quota this session ate.

chatgpt is a flat subscription, so 'cost' here is quota burned, not dollars. it
saves a baseline the first time and shows the delta after. reset with --reset.
reads only the numeric usage; never touches your email or id.
"""
import sys
import json
import time
import pathlib
import subprocess

AUTH = pathlib.Path.home() / ".codex" / "auth.json"
BASE = pathlib.Path.home() / "crabcab" / "session-cost.json"


def used_percent():
    try:
        t = json.loads(AUTH.read_text())["tokens"]
        _a, _c = str(t.get("access_token", "")), str(t.get("account_id", ""))
        if not _a or any((not ch.isprintable()) or ch in '"\\' for ch in (_a + _c)):
            return None
        cfg = "\n".join(['url = "https://chatgpt.com/backend-api/wham/usage"',
                         f'header = "Authorization: Bearer {_a}"',
                         f'header = "ChatGPT-Account-Id: {_c}"',
                         'header = "originator: codex_cli_rs"',
                         'proto = "=https"', 'tlsv1.2', 'max-redirs = "0"',
                         'silent', 'show-error', 'max-time = "8"']) + "\n"
        out = subprocess.run(["curl", "--config", "-"], input=cfg.encode(), capture_output=True).stdout
        d = json.loads(out.decode("utf-8", "replace"))
        return (d.get("rate_limit") or {}).get("primary_window", {}).get("used_percent")
    except Exception:
        return None


def main(reset=False):
    cur = used_percent()
    if cur is None:
        print("crabcab: couldn't read usage (online and signed in?).")
        return
    now = int(time.time())
    if reset or not BASE.exists():
        BASE.parent.mkdir(exist_ok=True)
        BASE.write_text(json.dumps({"used": cur, "ts": now}))
        print(f"crabcab: baseline set at {cur}% weekly quota used. run `crabcab-cost` later for the delta.")
        return
    b = json.loads(BASE.read_text())
    delta = max(0, cur - b.get("used", cur))
    mins = (now - b.get("ts", now)) // 60
    print("\ncrabcab: this session\n")
    print(f"  weekly quota burned:  {delta}%   ({b.get('used')}% -> {cur}%)")
    print(f"  session length:       ~{mins} min")
    print("  note: flat subscription, so this is quota used, not dollars. `--reset` to start fresh.\n")


if __name__ == "__main__":
    main(reset="--reset" in sys.argv)
