#!/usr/bin/env python3
"""crabcab: quietly check your chatgpt quota and spot resets.

runs at startup. prints at most one chill line, and only when there's real news -
never mid-session, never blocking. it can tell a normal scheduled reset from an
early or global one (the kind tibo sometimes triggers) purely from the timing,
watches tibo's tweets for a reset in basically any wording, checks the community
reset trackers, and fires a desktop notification when something actually happens.

safety: it only ever reads the numeric quota fields. your email, user id, and
account id from that endpoint are never touched, printed, or stored. the little
state file it keeps holds numbers only. every network call is read-only.
"""
import os
import sys
import json
import time
import shutil
import pathlib
import platform
import subprocess
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

AUTH = pathlib.Path.home() / ".codex" / "auth.json"
STATE = pathlib.Path.home() / "crabcab" / "usage-state.json"
HISTORY = pathlib.Path.home() / "crabcab" / "reset-history.jsonl"
TIBO_EVERY = 6 * 3600     # peek at nitter for tibo every 6h, tops
STATUS_EVERY = 30 * 60
NITTERS = ("nitter.net", "nitter.poast.org")
TRACKERS = ("https://hascodexratelimitreset.today/", "https://www.willcodexquotareset.com/")

# tibo's actual reset signatures - strong enough to fire on their own
STRONG = ("reset", "may the tokens flow", "let the tokens", "tokens flow again",
          "banked reset", "into your bank")
RESET_VERBS = ("bumped", "topped up", "topped everyone", "topped you", "cleared", "refreshed",
               "wiped", "restored", "replenished", "freed up", "fresh limit", "fresh usage", "fresh",
               "full tank", "back to full", "back to 100", "raised the limit", "raised limits",
               "lifted the limit", "boosted", "more usage", "extra usage", "extra credit", "banked",
               "gave everyone", "everyone gets", "unlimited", "rejoice", "credited")
CTX = ("limit", "usage", "codex", "quota", "cap", "rate", "5.6", "sol", "terra", "luna", "credit", "token")


def looks_like_reset(text):
    """does this tweet look like a codex/limits reset, however it's worded?"""
    t = " ".join(text.lower().split())
    if any(s in t for s in STRONG):
        return True
    return any(v in t for v in RESET_VERBS) and any(c in t for c in CTX)


def _curl(url, extra=None, timeout=8):
    cfg = [f'url = "{url}"', "silent", "show-error", f'max-time = "{timeout}"',
           'proto = "=https"', 'tlsv1.2', 'max-redirs = "0"',
           'max-filesize = "5000000"'] + (extra or [])
    p = subprocess.run(["curl", "--config", "-"], input=("\n".join(cfg) + "\n").encode(),
                       capture_output=True)
    return p.stdout.decode("utf-8", "replace")


def _clean(text):
    """strip control/escape chars from untrusted fetched text before it reaches a terminal or a
    desktop notification - blocks terminal-escape (ansi/osc) injection and applescript breakout."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:300]


def _notify(text):
    """fire a native desktop notification. off with CRABCAB_NOTIFY=0. fails silent."""
    if os.environ.get("CRABCAB_NOTIFY", "1") == "0":
        return
    msg = _clean(text)
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            subprocess.run(["osascript",
                            "-e", "on run argv",
                            "-e", 'display notification (item 1 of argv) with title "crabcab"',
                            "-e", "end run", "--", msg],
                           capture_output=True, timeout=5)
        elif sysname == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", "crabcab", msg], capture_output=True, timeout=5)
    except Exception:
        pass


def fetch_usage():
    try:
        toks = (json.loads(AUTH.read_text()).get("tokens") or {})
        access, account = toks.get("access_token"), toks.get("account_id")
        if not access:
            return None
        if any((not c.isprintable()) or c in '"\\' for c in (access + str(account or ""))):
            return None
        data = json.loads(_curl("https://chatgpt.com/backend-api/wham/usage", [
            f'header = "Authorization: Bearer {access}"',
            f'header = "ChatGPT-Account-Id: {account or ""}"',
            'header = "originator: codex_cli_rs"',
        ]))
        rl = data.get("rate_limit") or {}
        pw = rl.get("primary_window") or {}
        banked = (data.get("rate_limit_reset_credits") or {}).get("available_count", 0)
        return {"used_percent": pw.get("used_percent"), "reset_at": pw.get("reset_at"),
                "limit_reached": rl.get("limit_reached", False), "banked": banked}
    except Exception:
        return None


def _recent_tweets(rss_text, now, within=72 * 3600, cap=5):
    """newest tweet texts from a nitter rss feed, filtered to the last few days."""
    if "<!DOCTYPE" in rss_text or "<!ENTITY" in rss_text:
        return []
    try:
        root = ET.fromstring(rss_text)
    except Exception:
        return []
    texts = []
    for item in root.iter("item"):
        body = (item.findtext("title") or "") + " " + (item.findtext("description") or "")
        pd = item.findtext("pubDate")
        recent = True
        if pd:
            try:
                recent = (now - parsedate_to_datetime(pd).timestamp()) < within
            except Exception:
                recent = True
        if recent:
            texts.append(body)
        if len(texts) >= cap:
            break
    return texts


def check_tibo(now):
    """best-effort: did tibo tweet a reset lately? nitter rss, read-only, fails silent."""
    for host in NITTERS:
        try:
            rss = _curl(f"https://{host}/thsottiaux/rss", timeout=6)
            if "<item" not in rss:
                continue
            if any(looks_like_reset(t) for t in _recent_tweets(rss, now)):
                return "tibo just tweeted about a reset - peek x.com/thsottiaux"
            return None
        except Exception:
            continue
    return None


def check_trackers(now):
    """community reset trackers. they flip a 'verdict' to yes on reset. conservative, fails silent."""
    for url in TRACKERS:
        try:
            html = _curl(url, timeout=6).lower()
            if "verdict" not in html:
                continue
            if ((">yes<" in html or "verdict: yes" in html or "reset detected" in html)
                    and "no classification" not in html):
                return "a community reset tracker flipped to YES - codex limits look reset, check your quota"
        except Exception:
            continue
    return None


def check_status(now):
    """openai status: any active codex incident? tibo's resets often follow one. read-only."""
    try:
        d = json.loads(_curl("https://status.openai.com/api/v2/summary.json", timeout=6))
        for i in d.get("incidents", []):
            if i.get("status") != "resolved" and "codex" in (i.get("name") or "").lower():
                return "codex has an active incident on openai status - a reset sometimes follows, keep an eye out"
    except Exception:
        pass
    return None


def _log_history(kind, cur, prev):
    try:
        HISTORY.parent.mkdir(exist_ok=True)
        with HISTORY.open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "kind": kind,
                                "used_before": (prev or {}).get("used_percent"),
                                "used_after": cur.get("used_percent"),
                                "banked": cur.get("banked")}) + "\n")
    except Exception:
        pass


def show_history():
    if not HISTORY.exists():
        print("crabcab: no resets recorded yet.")
        return
    print("\ncrabcab: reset history\n")
    for line in HISTORY.read_text().splitlines():
        try:
            e = json.loads(line)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
            print(f"  {when}  {e['kind']:10} {e.get('used_before')}% -> {e.get('used_after')}%")
        except Exception:
            continue
    print()


def main():
    now = int(time.time())
    cur = fetch_usage()
    if not cur or cur.get("used_percent") is None:
        return

    try:
        prev = json.loads(STATE.read_text())
    except Exception:
        prev = None

    banked = cur.get("banked") or 0
    prev_banked = (prev or {}).get("banked", banked)
    msg = reset_kind = None
    if prev and prev.get("reset_at") and cur.get("reset_at"):
        if cur["reset_at"] > prev["reset_at"]:
            if now < prev["reset_at"] - 120:
                msg, reset_kind = "your limits got reset EARLY (looks like a global reset) - free quota, enjoy", "early"
            else:
                msg, reset_kind = "your quota window just reset - full tank", "scheduled"
        elif (isinstance(cur.get("used_percent"), (int, float))
              and isinstance(prev.get("used_percent"), (int, float))
              and cur["used_percent"] + 25 < prev["used_percent"]
              and now < prev["reset_at"] - 120):
            msg, reset_kind = "your usage just dropped a lot early - looks like a reset, more quota freed up", "early"

    # a banked reset just landed (tibo credits these; you spend them when you want)
    if msg is None and banked > prev_banked:
        msg, reset_kind = f"tibo just banked you a reset - you've got {banked} to spend whenever you want", "banked"

    last_tibo = (prev or {}).get("tibo_checked_at", 0)
    last_status = (prev or {}).get("status_checked_at", 0)
    last_track = (prev or {}).get("track_checked_at", 0)
    do_tibo = msg is None and (now - last_tibo > TIBO_EVERY)
    do_track = msg is None and (now - last_track > STATUS_EVERY)
    do_status = msg is None and (now - last_status > STATUS_EVERY)
    tibo = check_tibo(now) if do_tibo else None
    trackers = check_trackers(now) if (do_track and not tibo) else None
    status = check_status(now) if (do_status and not tibo and not trackers) else None

    try:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({
            "used_percent": cur["used_percent"], "reset_at": cur["reset_at"], "banked": banked,
            "checked_at": now, "tibo_checked_at": now if do_tibo else last_tibo,
            "status_checked_at": now if do_status else last_status,
            "track_checked_at": now if do_track else last_track,
        }))
    except Exception:
        pass

    if reset_kind:
        _log_history(reset_kind, cur, prev)

    left = 100 - int(cur["used_percent"])
    out = None
    if msg:
        out = f"{msg}  ({left}% quota left)"
    elif tibo:
        out = tibo
    elif trackers:
        out = trackers
    elif status:
        out = status
    elif cur.get("limit_reached") and banked > 0:
        out = f"you're maxed out - but you have {banked} banked reset(s), pop one to refill"
    elif cur.get("limit_reached"):
        out = "heads up, you're at your chatgpt limit right now - wait for the reset"
    elif int(cur["used_percent"]) >= 85 and banked > 0:
        out = f"you're at {int(cur['used_percent'])}% - you've got {banked} banked reset(s) when you need one"

    if out:
        print(_clean(f"crabcab: {out}"))
        _notify(f"crabcab: {out}")


if __name__ == "__main__":
    if "--history" in sys.argv:
        show_history()
    else:
        main()
