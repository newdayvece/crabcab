#!/usr/bin/env python3
"""crabcab: show how much of your chatgpt quota is left.

reads your token, asks the same usage endpoint codex uses, and prints ONLY the
rate-limit info. it deliberately never prints your email, user id, or account id -
that stuff stays on your machine.
"""
import json
import pathlib
import subprocess

AUTH = pathlib.Path.home() / ".codex" / "auth.json"


def main():
    try:
        d = json.loads(AUTH.read_text())
    except Exception:
        print("no codex login found (~/.codex/auth.json). run `codex` to sign in first.")
        return
    toks = d.get("tokens") or {}
    access, account = toks.get("access_token"), toks.get("account_id")
    if not access:
        print("no access token in ~/.codex/auth.json. run `codex` to sign in first.")
        return
    if any((not c.isprintable()) or c in '"\\' for c in (access + str(account or ""))):
        print("auth.json token/account has a control or quote char - refusing (possible tampering).")
        return

    cfg = "\n".join([
        'url = "https://chatgpt.com/backend-api/wham/usage"',
        f'header = "Authorization: Bearer {access}"',
        f'header = "ChatGPT-Account-Id: {account or ""}"',
        'header = "originator: codex_cli_rs"',
        'user-agent = "codex_cli_rs/0.144.1"',
        'proto = "=https"', 'tlsv1.2', 'max-redirs = "0"',
        'silent', 'show-error',
    ]) + "\n"
    p = subprocess.run(["curl", "--config", "-"], input=cfg.encode(), capture_output=True)
    try:
        data = json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        print("couldn't read usage (are you online and signed in?).")
        return

    plan = data.get("plan_type", "unknown")
    rl = data.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    used = pw.get("used_percent")
    reset_after = pw.get("reset_after_seconds")
    banked = (data.get("rate_limit_reset_credits") or {}).get("available_count", 0)

    print("\ncrabcab: chatgpt quota\n")
    print(f"  plan:       {plan}")
    if isinstance(used, (int, float)):
        left = max(0, 100 - int(used))
        filled = left // 10
        bar = "#" * filled + "-" * (10 - filled)
        print(f"  used:       {int(used)}%   [{bar}]  {left}% left")
    if rl.get("limit_reached"):
        print("  status:     limit reached - wait for the reset below")
    if isinstance(reset_after, (int, float)):
        print(f"  resets in:  ~{reset_after / 3600:.1f} h")
    if banked:
        print(f"  banked:     {banked} reset(s) saved - spend one whenever you want")
    print()


if __name__ == "__main__":
    main()
