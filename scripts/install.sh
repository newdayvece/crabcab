#!/usr/bin/env bash
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "crabcab installer"
echo

python3 "$SRC/scripts/doctor.py" || { echo "fix the items above, then re-run."; exit 1; }

mkdir -p "$HOME/crabcab"
cp "$SRC/scripts/"*.py "$HOME/crabcab/"
echo "copied scripts -> ~/crabcab"

python3 - <<'PY'
import json, os, pathlib, tempfile
p = pathlib.Path.home() / ".claude" / "settings.json"
p.parent.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(p.parent, 0o700)
except OSError:
    pass
try:
    d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not isinstance(d, dict):
        raise ValueError("settings root is not an object")
except Exception as exc:
    raise SystemExit(f"refusing to overwrite malformed {p}: {exc}")
if not d.get("model"):
    d["model"] = "gpt-5.6-sol"
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".settings-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        os.chmod(p, 0o600)
        dfd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print("seeded default model gpt-5.6-sol into ~/.claude/settings.json")
else:
    print("~/.claude/settings.json already pins model: %s (left as-is)" % d["model"])
PY

echo
echo "offline check:"
python3 "$HOME/crabcab/selftest.py" | tail -1

case "$(basename "${SHELL:-/bin/zsh}")" in
  zsh)  RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bashrc" ;;
  *)    RC="$HOME/.profile" ;;
esac
LAUNCHER="$SRC/references/launcher.zsh"

echo
if grep -q "crabcab: claude code on a gpt model" "$RC" 2>/dev/null; then
  echo "launcher already in $RC (leaving it)."
else
  printf "add the claudex launcher to %s ? [y/N] " "$RC"
  read -r ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    { echo; cat "$LAUNCHER"; } >> "$RC"
    echo "added. run:  source $RC"
  else
    echo "skipped. the block lives at $LAUNCHER - paste it into $RC when ready."
  fi
fi

echo
echo "done. start it with:  claudex   (then /model shows your tier; switch live with /model sol|terra|luna)"
echo "handy: crabcab-usage  crabcab-status  crabcab-tier  crabcab-cost  crabcab-verify  crabcab-history  crabcab-stop"
