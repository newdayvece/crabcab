#!/usr/bin/env python3
"""crabcab verify: offline selftest + login check + a real live ping. reports pass/fail."""
import os
import sys
import json
import time
import pathlib
import tempfile
import secrets
import subprocess

HERE = pathlib.Path(__file__).resolve().parent


def _ok(passed, label):
    print(f"  [{'ok  ' if passed else 'FAIL'}] {label}")
    return passed


def main():
    print("\ncrabcab verify\n")
    ok = True

    r = subprocess.run([sys.executable, str(HERE / "selftest.py")], capture_output=True, text=True)
    ok &= _ok("all green" in r.stdout.lower(), "offline translation selftest")

    auth = pathlib.Path.home() / ".codex" / "auth.json"
    has_login = False
    try:
        has_login = bool((json.loads(auth.read_text()).get("tokens") or {}).get("access_token"))
    except Exception:
        pass
    ok &= _ok(has_login, "codex login present")

    sec = secrets.token_urlsafe(24)
    port = str(20000 + secrets.randbelow(40000))
    rlog = pathlib.Path(tempfile.gettempdir()) / "crabcab-verify-routing.log"
    try:
        rlog.unlink()
    except OSError:
        pass
    env = dict(os.environ, CRABCAB_PORT=port, CRABCAB_ROUTING_LOG=str(rlog), CRABCAB_SECRET=sec)
    proc = subprocess.Popen([sys.executable, "-u", str(HERE / "proxy.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            if subprocess.run(["curl", "-s", "-o", "/dev/null", base + "/"], capture_output=True).returncode == 0:
                break
            time.sleep(0.1)

        def _code(*a):
            return subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", *a], capture_output=True, text=True).stdout
        K = ["-H", f"x-api-key: {sec}"]
        ok &= _ok(_code(base + "/v1/models", *K) == "200", "exact route /v1/models -> 200")
        ok &= _ok(_code(base + "/v1/models-bogus", *K) == "404", "prefix-collision /v1/models-bogus -> 404")
        ok &= _ok(_code("-X", "POST", base + "/v1/messages-x", *K, "-H", "content-type: application/json", "--data", "{}") == "404", "prefix-collision POST route -> 404")
        ok &= _ok(_code("-X", "POST", base + "/v1/messages", *K, "-H", "content-type: application/json", "--data", "[]") == "400", "top-level non-object body -> 400")
        ok &= _ok(_code(base + "/v1/crabcab-id?n=short") == "400", "short challenge nonce -> 400")
        ok &= _ok(_code(base + "/v1/crabcab-id?n=" + "a" * 20) == "200", "valid challenge nonce -> 200")

        body = '{"model":"terra","max_tokens":15,"stream":true,"messages":[{"role":"user","content":"say ok"}]}'
        out = subprocess.run(["curl", "-sN", "--max-time", "45", base + "/v1/messages",
                              "-H", "content-type: application/json",
                              "-H", f"x-api-key: {sec}", "-d", body],
                             capture_output=True, text=True).stdout
        ok &= _ok("text_delta" in out, "live gpt reply through the proxy")
        routed = ""
        try:
            routed = rlog.read_text().strip().splitlines()[-1].split()[-1]
        except Exception:
            pass
        ok &= _ok(routed == "gpt-5.6-terra", f"bare tier slug 'terra' actually routed to gpt-5.6-terra (backend saw: {routed or 'nothing'})")
    finally:
        proc.terminate()
        proc.wait()
        try:
            rlog.unlink()
        except OSError:
            pass

    print("\n  " + ("healthy - crabcab is good to go" if ok else "some checks failed, see above") + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
