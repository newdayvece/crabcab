# crabcab: other shells, config, and auto-start

the zsh/bash launcher lives in `launcher.zsh`. here are the rest.

## config file (optional)

drop a `~/.crabcab.conf` to set your defaults - the launcher sources it:
```bash
CRABCAB_MODEL=gpt-5.6-terra    # default tier
CRABCAB_EFFORT=high            # low | medium | high | xhigh | max
CRABCAB_PORT=8317              # change if 8317 is taken
```

## fish

`~/.config/fish/config.fish`:
```fish
function claudex
  test -f "$HOME/.crabcab.conf"; and source "$HOME/.crabcab.conf"
  set -l model $CRABCAB_MODEL
  switch "$argv[1]"
    case sol terra luna; set model "gpt-5.6-$argv[1]"; set argv $argv[2..-1]
    case 'gpt-*' 'claude-gpt-*'; set model $argv[1]; set argv $argv[2..-1]
  end
  test -s "$HOME/crabcab/reset-news.txt"; and cat "$HOME/crabcab/reset-news.txt"; and echo -n > "$HOME/crabcab/reset-news.txt"
  test -s "$HOME/crabcab/.secret"; or begin; mkdir -p "$HOME/crabcab"; sh -c 'umask 077; head -c 32 /dev/urandom | base64 | tr -d "\n" > "$HOME/crabcab/.secret"'; end
  set -l secret $CRABCAB_SECRET
  test -n "$secret"; or set secret (cat "$HOME/crabcab/.secret")
  set -l nonce (head -c 16 /dev/urandom | base64 | tr -d '/+=\n')
  set -l expected (printf '%s' "$secret" | python3 -c 'import hmac,hashlib,sys;print(hmac.new(sys.stdin.buffer.read(),sys.argv[1].encode(),hashlib.sha256).hexdigest())' "$nonce")
  set -l claimed (curl -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>/dev/null | grep -o '[a-f0-9]\{64\}')
  if test "$claimed" != "$expected"
    echo "starting crabcab..."
    pkill -f "crabcab/proxy.py" 2>/dev/null; sleep 0.2
    nohup python3 "$HOME/crabcab/proxy.py" > "$HOME/crabcab/proxy.log" 2>&1 &
    for i in (seq 50); set claimed (curl -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>/dev/null | grep -o '[a-f0-9]\{64\}'); test "$claimed" = "$expected"; and break; sleep 0.1; end
    test "$claimed" != "$expected"; and begin; echo "crabcab: 127.0.0.1:8317 is held by something that isn't yours - refusing to send your key."; return 1; end
  end
  fish -c "python3 $HOME/crabcab/reset-watch.py > $HOME/crabcab/reset-news.txt 2>/dev/null &"
  # only force --model when you name a tier; otherwise your last /model pick sticks
  set -l margs
  test -n "$model"; and set margs --model $model
  env ANTHROPIC_BASE_URL=http://127.0.0.1:8317 ANTHROPIC_API_KEY=$secret CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1 claude $margs $argv
end
function crabcab-stop; pkill -f "crabcab/proxy.py"; and echo "crabcab stopped"; end
function crabcab-usage; python3 "$HOME/crabcab/usage.py"; end
function crabcab-tier; tail -n 8 "$HOME/crabcab/routing.log" 2>/dev/null; or echo "no requests logged yet - send a message first"; end
```

## powershell (windows)

`$PROFILE`:
```powershell
function claudex {
  if (Test-Path "$HOME\.crabcab.conf") {
    Get-Content "$HOME\.crabcab.conf" | ForEach-Object { if ($_ -match '^\s*([A-Za-z_]+)=(.*)$') { Set-Item "env:$($matches[1])" $matches[2].Trim() } }
  }
  $model = $env:CRABCAB_MODEL
  if     ($args.Count -and $args[0] -in "sol","terra","luna")                          { $model = "gpt-5.6-$($args[0])"; $args = $args[1..($args.Count-1)] }
  elseif ($args.Count -and ($args[0] -like "gpt-*" -or $args[0] -like "claude-gpt-*")) { $model = $args[0];              $args = $args[1..($args.Count-1)] }
  $news = "$HOME\crabcab\reset-news.txt"
  if ((Test-Path $news) -and (Get-Item $news).Length -gt 0) { Get-Content $news; Clear-Content $news }
  # per-machine secret so no other local user can spend your quota
  $secretFile = "$HOME\crabcab\.secret"
  if (-not (Test-Path $secretFile) -or -not (Get-Content -Raw $secretFile -ErrorAction SilentlyContinue).Trim()) {
    New-Item -ItemType Directory -Force -Path "$HOME\crabcab" | Out-Null
    $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -NoNewline -Path $secretFile
  }
  $secret = if ($env:CRABCAB_SECRET) { $env:CRABCAB_SECRET } else { (Get-Content -Raw $secretFile).Trim() }
  # (powershell computes the HMAC in-process via .NET below, so the secret is never passed on a command line)
  # prove the proxy on :8317 is really OURS (holds our secret) before handing it the key - defeats a port squatter
  $nb = New-Object byte[] 16; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($nb)
  $nonce = ([Convert]::ToBase64String($nb) -replace '[/+=]','')
  $h = [System.Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($secret))
  $expected = ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($nonce))) -replace '-','').ToLower()
  $resp = (& curl.exe -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>$null)
  $claimed = if ($resp -match '([a-f0-9]{64})') { $matches[1] } else { '' }
  if ($claimed -ne $expected) {
    Write-Host "starting crabcab..."
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" | Where-Object { $_.CommandLine -like "*crabcab*proxy.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    $py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source; if (-not $py) { $py = "python" }
    Start-Process -FilePath $py -ArgumentList "$HOME\crabcab\proxy.py" -WindowStyle Hidden -RedirectStandardError "$HOME\crabcab\proxy.log"
    for ($i=0; $i -lt 50; $i++) { $resp = (& curl.exe -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>$null); $claimed = if ($resp -match '([a-f0-9]{64})') { $matches[1] } else { '' }; if ($claimed -eq $expected) { break }; Start-Sleep -Milliseconds 100 }
    if ($claimed -ne $expected) { Write-Host "crabcab: 127.0.0.1:8317 is held by something that isn't yours - refusing to send your key."; return }
  }
  Start-Process -FilePath python -ArgumentList "$HOME\crabcab\reset-watch.py" -WindowStyle Hidden -RedirectStandardOutput "$HOME\crabcab\reset-news.txt"
  $env:ANTHROPIC_BASE_URL="http://127.0.0.1:8317"
  $env:ANTHROPIC_API_KEY=$secret
  $env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT="1"
  # only force --model when you name a tier; otherwise your last /model pick sticks (settings.json)
  if ($model) { claude --model $model @args } else { claude @args }
}
function crabcab-stop { Get-CimInstance Win32_Process -Filter "Name like '%python%'" | Where-Object { $_.CommandLine -like "*crabcab*proxy.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host "crabcab stopped" }
function crabcab-usage { python "$HOME\crabcab\usage.py" }
function crabcab-tier  { if (Test-Path "$HOME\crabcab\routing.log") { Get-Content "$HOME\crabcab\routing.log" -Tail 8 } else { "no requests logged yet - send a message first" } }
```

### windows notes
- claude code is native on windows now (no wsl needed): `irm https://claude.ai/install.ps1 | iex`, or `winget install Anthropic.ClaudeCode`.
- codex login lives at `%USERPROFILE%\.codex\auth.json`; the proxy finds it automatically via the home dir.
- use `python` (not `python3`); `pythonw` runs the proxy with no console window.
- windows 10/11 ship `curl.exe` (which the proxy shells out to), and settings live at `%USERPROFILE%\.claude\settings.json`.
- the proxy is pure standard-library python and binds `127.0.0.1` only - same safety on every os.

## the local gate (on by default) + rotating the secret

crabcab is locked to you out of the box: the proxy auto-creates `~/crabcab/.secret` (`0600`) and requires
it on every request; the launcher passes it to claude via `ANTHROPIC_API_KEY`. so even on a shared /
multi-user box, no other local user can hit `127.0.0.1:8317` and spend your quota - they just get `403`.

- **rotate the secret:** delete `~/crabcab/.secret` and relaunch; a fresh one is generated.
- **manual `curl` tests** need the header: `-H "x-api-key: $(cat ~/crabcab/.secret)"`.
- setting `ANTHROPIC_API_KEY` means claude uses its api path, so claude.ai connectors are off in a
  crabcab session - fine, crabcab runs on the gpt backend, not connectors.

## auto-start on boot (macos, optional)

so the proxy is always up, no manual start. copy `com.crabcab.proxy.plist` to
`~/Library/LaunchAgents/`, replace `REPLACE_HOME` with your home path, then:
```bash
sed -i '' "s#REPLACE_HOME#$HOME#g" ~/Library/LaunchAgents/com.crabcab.proxy.plist
launchctl load ~/Library/LaunchAgents/com.crabcab.proxy.plist
```
`crabcab-stop` still works; to disable auto-start, `launchctl unload` the same path (the uninstaller does this for you).

on linux use a systemd user service pointing `ExecStart` at `python3 ~/crabcab/proxy.py`.
