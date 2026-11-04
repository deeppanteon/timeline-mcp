#!/usr/bin/env bash
# Runs Timeline desk (dashboard.py) as a systemd *user* service, so it starts
# at login and restarts on failure: http://timeline.localhost:8765
# No sudo needed. Linux with systemd only.
#
#   scripts/dashboard-service.sh install     # write the unit, enable and start it
#   scripts/dashboard-service.sh uninstall   # stop, disable and remove it
#   scripts/dashboard-service.sh status      # state and recent log lines
#   scripts/dashboard-service.sh restart     # pick up code changes
set -euo pipefail

NAME="timeline-desk"
PORT="${TIMELINE_DESK_PORT:-8765}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"
PYTHON="$REPO/.venv/bin/python"

install_unit() {
  if [[ ! -x "$PYTHON" ]]; then
    echo "Missing $PYTHON. Create the venv first (see README: Setup)." >&2
    exit 1
  fi
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=Timeline desk (Panteon Timeline dashboard on http://timeline.localhost:$PORT)
After=network-online.target

[Service]
WorkingDirectory=$REPO
ExecStart="$PYTHON" "$REPO/backend/dashboard.py" --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$NAME.service"
  echo "Installed $UNIT"
  echo "Open http://timeline.localhost:$PORT"
}

case "${1:-}" in
  install) install_unit ;;
  uninstall)
    systemctl --user disable --now "$NAME.service" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Removed $NAME.service"
    ;;
  status)
    systemctl --user status "$NAME.service" --no-pager || true
    ;;
  restart) systemctl --user restart "$NAME.service" ;;
  *)
    echo "Usage: $0 install|uninstall|status|restart" >&2
    exit 2
    ;;
esac
