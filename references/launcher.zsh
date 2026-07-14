# crabcab: claude code on a gpt model via your chatgpt sub  (claudex | claudex terra|luna | crabcab-usage/status/stop/cost/verify/history)
claudex() {
  [ -f "$HOME/.crabcab.conf" ] && source "$HOME/.crabcab.conf"
  local model="$CRABCAB_MODEL"
  case "$1" in sol|terra|luna) model="gpt-5.6-$1"; shift ;; gpt-*|claude-gpt-*) model="$1"; shift ;; esac
  [ -s "$HOME/crabcab/reset-news.txt" ] && { cat "$HOME/crabcab/reset-news.txt"; : > "$HOME/crabcab/reset-news.txt"; }
  [ -s "$HOME/crabcab/.secret" ] || { mkdir -p "$HOME/crabcab"; ( umask 077; head -c 32 /dev/urandom | base64 | tr -d '\n' > "$HOME/crabcab/.secret" ); }
  local secret nonce expected
  secret="${CRABCAB_SECRET:-$(cat "$HOME/crabcab/.secret")}"
  nonce="$(head -c 16 /dev/urandom | base64 | tr -d '/+=\n')"
  expected="$(printf '%s' "$secret" | python3 -c 'import hmac,hashlib,sys;print(hmac.new(sys.stdin.buffer.read(),sys.argv[1].encode(),hashlib.sha256).hexdigest())' "$nonce")"
  if [ "$(curl -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>/dev/null | grep -o '[a-f0-9]\{64\}')" != "$expected" ]; then
    echo "starting crabcab..."
    pkill -f "crabcab/proxy.py" 2>/dev/null; sleep 0.2
    nohup python3 "$HOME/crabcab/proxy.py" >"$HOME/crabcab/proxy.log" 2>&1 &
    local i; for i in {1..50}; do [ "$(curl -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>/dev/null | grep -o '[a-f0-9]\{64\}')" = "$expected" ] && break; sleep 0.1; done
    if [ "$(curl -s --max-time 2 "http://127.0.0.1:8317/v1/crabcab-id?n=$nonce" 2>/dev/null | grep -o '[a-f0-9]\{64\}')" != "$expected" ]; then
      echo "crabcab: 127.0.0.1:8317 is held by something that isn't yours - refusing to hand over your key. run crabcab-stop or check for a port squatter."; return 1
    fi
  fi
  ( python3 "$HOME/crabcab/reset-watch.py" > "$HOME/crabcab/reset-news.txt" 2>/dev/null & )
  local -a margs; [ -n "$model" ] && margs=(--model "$model")
  ANTHROPIC_BASE_URL="http://127.0.0.1:8317" \
  ANTHROPIC_API_KEY="$secret" \
  CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1 \
  claude "${margs[@]}" "$@"
}
crabcab-stop()    { pkill -f "crabcab/proxy.py" && echo "crabcab stopped"; }
crabcab-status()  { if curl -s -o /dev/null http://127.0.0.1:8317/ 2>/dev/null; then echo "crabcab: up on :8317 (model ${CRABCAB_MODEL:-gpt-5.6-sol})"; else echo "crabcab: not running - start it with claudex"; fi; }
crabcab-usage()   { python3 "$HOME/crabcab/usage.py"; }
crabcab-cost()    { python3 "$HOME/crabcab/cost.py" "$@"; }
crabcab-verify()  { python3 "$HOME/crabcab/verify.py"; }
crabcab-history() { python3 "$HOME/crabcab/reset-watch.py" --history; }
crabcab-tier()    { tail -n "${1:-8}" "$HOME/crabcab/routing.log" 2>/dev/null || echo "no requests logged yet - send a message in claudex first"; }
