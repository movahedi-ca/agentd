#!/bin/sh
# agentd installer: copies the daemon into ~/.local and prints your token.
# Zero dependencies beyond python3. No network access, no telemetry.
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
APP="$PREFIX/share/agentd/app"
BIN="$PREFIX/bin"

echo "Installing agentd to $APP ..."

mkdir -p "$APP" "$BIN"

# The python package (stdlib only).
rm -rf "$APP/agentd"
cp -r "$(dirname "$0")/agentd" "$APP/agentd"
find "$APP/agentd" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Launcher with the install location baked in.
sed "s|__AGENTD_LIB_DEFAULT__|$APP|" \
  "$(dirname "$0")/bin/agentd" > "$BIN/agentd"
chmod +x "$BIN/agentd"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on your PATH. Add it, e.g.:"; echo "  export PATH=\"$BIN:\$PATH\"";;
esac

if [ -n "${AGENTD_TOKEN:-}" ]; then
  echo "AGENTD_TOKEN is set in the environment; using it instead of a token file."
  echo "Unset it and run 'agentd rotate-token' to switch to a stored token."
  TOKEN="\$AGENTD_TOKEN"
else
  TOKEN="$("$BIN/agentd" rotate-token)"
  echo
  echo "Installed. Your bearer token (shown once, stored 0600 at ~/.config/agentd/token):"
  echo "  $TOKEN"
fi
echo
echo "Start the daemon:"
echo "  agentd start"
echo "Then create a session:"
echo "  curl -s -H \"Authorization: Bearer $TOKEN\" \\"
echo "    -H 'Content-Type: application/json' -d '{\"provider\":\"claude\"}' \\"
echo "    http://127.0.0.1:8765/v1/sessions"
echo
echo "The daemon only listens on 127.0.0.1 and shells out to the agent CLIs"
echo "you already installed (claude, codex) under your own subscription."
