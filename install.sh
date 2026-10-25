#!/usr/bin/env bash
# TV Ambilight installer for Debian / Ubuntu / Raspberry Pi OS.
# Installs adb, creates ambilight.env from the example and an always-on systemd service for this user.
# Usage:  ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"

echo "==> Installing adb and Python (needs sudo)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq adb python3 avahi-daemon libnss-mdns >/dev/null

if [ ! -f "$DIR/ambilight.env" ]; then
  cp "$DIR/ambilight.env.example" "$DIR/ambilight.env"
  echo "==> Created $DIR/ambilight.env - edit it with your TV and WLED addresses"
fi

echo "==> Installing systemd service tv-ambilight (runs as $USER_NAME)"
sudo tee /etc/systemd/system/tv-ambilight.service >/dev/null <<EOF
[Unit]
Description=TV Ambilight: TV grabber receiver, LED processing, WLED output, web page and watchdog
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR/server
EnvironmentFile=$DIR/ambilight.env
Environment=HOME=$HOME
ExecStart=/usr/bin/python3 -u $DIR/server/live_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable tv-ambilight >/dev/null

cat <<EOF

Done. Next steps:
  1. Edit $DIR/ambilight.env  (TV address, WLED address, LED layout)
  2. Set the web page login:   python3 $DIR/server/set_password.py you@example.com
  3. Start it:                 sudo systemctl restart tv-ambilight
  4. Watch the log:            journalctl -u tv-ambilight -f
  5. Open the page:            http://$(hostname -I | awk '{print $1}'):8765
EOF
