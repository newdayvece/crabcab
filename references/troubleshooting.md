# crabcab troubleshooting

first move for almost anything: start the proxy with debug on and read the logs.
```bash
CRABCAB_DEBUG=1 python3 ~/crabcab/proxy.py     # run it with debug on
tail -f ~/crabcab/proxy.log                    # server side (another terminal)
cat  ~/crabcab/debug.log                        # request shapes only, no secrets
```

## 401 / unauthorized / auth errors
your codex login token expired. fix: run `codex` once (or `codex login`) to refresh it. crabcab reads the fresh token automatically, no restart needed.

## "unsupported parameter: x"
the codex backend rejected a field claude code sent. crabcab already drops the known one (`max_output_tokens`). if a new one shows up: open `~/crabcab/proxy.py`, find `anthropic_to_responses(...)`, and stop forwarding that field. run with `CRABCAB_DEBUG=1` to see which one.

## "system messages are not allowed"
already handled: claude code interleaves `role:"system"` reminders and the backend forbids them, so crabcab remaps them to `role:"developer"`. if you still see this, your `proxy.py` is an old copy, so re-copy the current one.

## "requires a newer version of codex"
the model you picked needs a newer codex than you have. run `python3 scripts/update-codex.py`. it tells you the exact update command for your install (brew, npm, standalone, winget, scoop) and changes nothing. update codex, then try again. crabcab reports your installed codex version automatically, so once codex is current it just works.

## empty reply, or images ignored
pasted images are a deferred feature. crabcab drops image blocks for now (text still works fine). not a crash.

## port 8317 already in use
either `crabcab-stop`, or run on another port and update your launcher:
```bash
CRABCAB_PORT=8399 python3 ~/crabcab/proxy.py
# then change 127.0.0.1:8317 -> :8399 in your claudex function
```

## `claude -p` hangs for a long time
claude code retries on server errors, so a persistent upstream error looks like a hang. stop it and check `~/crabcab/proxy.log` for the real error.

## "bad file descriptor" / python can't reach https
crabcab uses `curl` for the outbound hop on purpose, to dodge flaky python ssl setups. if you hit this, make sure `curl` is installed and on your path (the doctor checks it).

## the quota / reset watcher never says anything
that's usually correct. it stays silent unless there's real news: a reset, a banked reset landing, or you're maxed out. to see your quota and bank anytime, run `python3 scripts/usage.py`. the tibo-tweet check is best-effort via nitter (often down); the reliable signal is your own usage resetting, which crabcab reads straight from your account.

## windows notes
- use `python` (not `python3`) and the powershell `$PROFILE` for the launcher.
- codex login lives at `%USERPROFILE%\.codex\auth.json`.
- background the proxy with `Start-Process -WindowStyle Hidden python "$HOME\crabcab\proxy.py"`.

## still stuck?
ask claude code: "read ~/crabcab/proxy.log and the last lines of ~/crabcab/debug.log and tell me what's failing." the debug log shows request structure only, never your token.
